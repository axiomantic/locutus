# src/locutus.nim
# High-performance, single-binary inter-assistant communication bus over Redis.
# Embeds Lua scripts at compile time and utilizes EVALSHA caching with automatic EVAL fallback.

import std/[
  os, osproc, strutils, json, openssl, sha1,
  times, random, streams, options, base64, tables
]
import config, resp

# OpenSSL C-bindings for native cryptographic operations
type
  EVP_CIPHER_CTX = pointer
  EVP_CIPHER = pointer

proc HMAC*(evp_md: EVP_MD, key: pointer, key_len: cint, d: cstring, n: csize_t, md: pointer, md_len: ptr cuint): cstring {.cdecl, importc: "HMAC", dynlib: DLLUtilName.}
proc CRYPTO_memcmp*(a: pointer, b: pointer, len: csize_t): cint {.cdecl, importc: "CRYPTO_memcmp", dynlib: DLLUtilName.}
proc RAND_bytes*(buf: pointer, num: cint): cint {.cdecl, importc: "RAND_bytes", dynlib: DLLUtilName.}

proc EVP_CIPHER_CTX_new*(): EVP_CIPHER_CTX {.cdecl, importc: "EVP_CIPHER_CTX_new", dynlib: DLLUtilName.}
proc EVP_CIPHER_CTX_free*(ctx: EVP_CIPHER_CTX) {.cdecl, importc: "EVP_CIPHER_CTX_free", dynlib: DLLUtilName.}
proc EVP_aes_256_cbc*(): EVP_CIPHER {.cdecl, importc: "EVP_aes_256_cbc", dynlib: DLLUtilName.}

proc EVP_EncryptInit_ex*(ctx: EVP_CIPHER_CTX, cipher: EVP_CIPHER, impl: pointer, key: pointer, iv: pointer): cint {.cdecl, importc: "EVP_EncryptInit_ex", dynlib: DLLUtilName.}
proc EVP_EncryptUpdate*(ctx: EVP_CIPHER_CTX, outbuf: pointer, outlen: ptr cint, inbuf: pointer, inlen: cint): cint {.cdecl, importc: "EVP_EncryptUpdate", dynlib: DLLUtilName.}
proc EVP_EncryptFinal_ex*(ctx: EVP_CIPHER_CTX, outbuf: pointer, outlen: ptr cint): cint {.cdecl, importc: "EVP_EncryptFinal_ex", dynlib: DLLUtilName.}

proc EVP_DecryptInit_ex*(ctx: EVP_CIPHER_CTX, cipher: EVP_CIPHER, impl: pointer, key: pointer, iv: pointer): cint {.cdecl, importc: "EVP_DecryptInit_ex", dynlib: DLLUtilName.}
proc EVP_DecryptUpdate*(ctx: EVP_CIPHER_CTX, outbuf: pointer, outlen: ptr cint, inbuf: pointer, inlen: cint): cint {.cdecl, importc: "EVP_DecryptUpdate", dynlib: DLLUtilName.}
proc EVP_DecryptFinal_ex*(ctx: EVP_CIPHER_CTX, outbuf: pointer, outlen: ptr cint): cint {.cdecl, importc: "EVP_DecryptFinal_ex", dynlib: DLLUtilName.}

proc PKCS5_PBKDF2_HMAC*(pass: cstring, passlen: cint, salt: pointer, saltlen: cint, iter: cint, digest: EVP_MD, keylen: cint, outbuf: pointer): cint {.cdecl, importc: "PKCS5_PBKDF2_HMAC", dynlib: DLLUtilName.}

# Compile-time embedded Lua scripts
const
  registerLua*   = staticRead("../scripts/register.lua")
  sendO2oLua*    = staticRead("../scripts/send_o2o.lua")
  multicastLua*  = staticRead("../scripts/multicast.lua")
  directoryLua*  = staticRead("../scripts/directory.lua")
  drainLua*      = staticRead("../scripts/drain.lua")
  tagLua*        = staticRead("../scripts/tag.lua")
  unregisterLua* = staticRead("../scripts/unregister.lua")
  statusLua*     = staticRead("../scripts/status.lua")
  lockLua*       = staticRead("../scripts/lock.lua")
  unlockLua*     = staticRead("../scripts/unlock.lua")
  enqueueLua*    = staticRead("../scripts/enqueue.lua")
  LocutusVersion* = "0.1.1"

# Cryptographic Helpers
proc computeSha1*(text: string): string =
  ($secureHash(text)).toLowerAscii

# Precomputed SHA1 hashes for Redis EVALSHA caching
let
  registerSha*   = computeSha1(registerLua)
  sendO2oSha*    = computeSha1(sendO2oLua)
  multicastSha*  = computeSha1(multicastLua)
  directorySha*  = computeSha1(directoryLua)
  drainSha*      = computeSha1(drainLua)
  tagSha*        = computeSha1(tagLua)
  unregisterSha* = computeSha1(unregisterLua)
  statusSha*     = computeSha1(statusLua)
  lockSha*       = computeSha1(lockLua)
  unlockSha*     = computeSha1(unlockLua)
  enqueueSha*    = computeSha1(enqueueLua)


proc secureFilePermissions*(path: string) =
  when not defined(windows):
    try:
      setFilePermissions(path, {fpUserRead, fpUserWrite})
    except CatchableError:
      discard

proc getOpenSslExe*(): string =
  let envExe = getEnv("OPENSSL_BIN", "")
  if envExe.len > 0 and fileExists(envExe):
    return envExe
  let found = findExe("openssl")
  if found.len > 0:
    return found
  when defined(windows):
    for candidate in [
      r"C:\Program Files\Git\usr\bin\openssl.exe",
      r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
      r"C:\OpenSSL-Win64\bin\openssl.exe"
    ]:
      if fileExists(candidate):
        return candidate
  return "openssl"

proc getSecret*(cfg: LocutusConfig = LocutusConfig()): string =
  if cfg.secret.len > 0:
    return cfg.secret
  let envSecret = getEnv("LOCUTUS_SECRET", "")
  if envSecret.len > 0:
    return envSecret
  let secretFile = if cfg.secretFile.len > 0:
    cfg.secretFile
  else:
    let secretFileEnv = getEnv("LOCUTUS_SECRET_FILE", "")
    let home = getHomeDir()
    let configDir = home / ".config" / "locutus"
    if secretFileEnv.len > 0: secretFileEnv else: configDir / "secret"
  if fileExists(secretFile):
    return readFile(secretFile).strip()

  createDir(secretFile.splitPath.head)
  var bytes: array[32, uint8]
  discard RAND_bytes(bytes[0].addr, 32)
  var hexSecret = ""
  for b in bytes:
    hexSecret.add(toHex(b.int, 2).toLowerAscii)
  writeFile(secretFile, hexSecret)
  secureFilePermissions(secretFile)
  return hexSecret

proc computeHmacSha256*(secret, data: string): string =
  var md: array[64, uint8]
  var mdLen: cuint = 0
  let mdPtr = EVP_sha256()
  discard HMAC(mdPtr, secret.cstring, secret.len.cint, data.cstring, data.len.csize_t, md[0].addr, mdLen.addr)
  result = newStringOfCap(mdLen.int * 2)
  for i in 0 ..< mdLen.int:
    result.add(toHex(md[i].int, 2).toLowerAscii)

proc verifyHmac*(secret, data, expectedSig: string): bool =
  if expectedSig.len == 0:
    return false
  let computed = computeHmacSha256(secret, data)
  if computed.len != expectedSig.len:
    return false
  return CRYPTO_memcmp(computed.cstring, expectedSig.cstring, computed.len.csize_t) == 0

proc getPassArg*(cfg: LocutusConfig = LocutusConfig()): string =
  if cfg.secret.len > 0:
    putEnv("LOCUTUS_SECRET", cfg.secret)
    return "env:LOCUTUS_SECRET"
  let envSecret = getEnv("LOCUTUS_SECRET", "")
  if envSecret.len > 0:
    return "env:LOCUTUS_SECRET"
  let secretFile = if cfg.secretFile.len > 0:
    cfg.secretFile
  else:
    let secretFileEnv = getEnv("LOCUTUS_SECRET_FILE", "")
    let home = getHomeDir()
    let configDir = home / ".config" / "locutus"
    if secretFileEnv.len > 0: secretFileEnv else: configDir / "secret"
  if fileExists(secretFile):
    return "file:" & secretFile
  discard getSecret(cfg)
  return "file:" & secretFile

proc encryptAes*(plaintext, secret: string, cfg: LocutusConfig = LocutusConfig()): string =
  var salt: array[8, uint8]
  if RAND_bytes(salt[0].addr, 8) != 1:
    raise newException(ValueError, "Failed to generate cryptographically secure random salt")

  var keyAndIv: array[48, uint8]
  let md = EVP_sha256()
  if PKCS5_PBKDF2_HMAC(secret.cstring, secret.len.cint, salt[0].addr, 8, 10000, md, 48, keyAndIv[0].addr) != 1:
    raise newException(ValueError, "PBKDF2 key derivation failed")

  let keyPtr = keyAndIv[0].addr
  let ivPtr = keyAndIv[32].addr

  let ctx = EVP_CIPHER_CTX_new()
  if ctx == nil:
    raise newException(ValueError, "Failed to create EVP_CIPHER_CTX")
  try:
    if EVP_EncryptInit_ex(ctx, EVP_aes_256_cbc(), nil, keyPtr, ivPtr) != 1:
      raise newException(ValueError, "EVP_EncryptInit_ex failed")

    var cipherBuf = newString(plaintext.len + 32)
    var outLen1: cint = 0
    if EVP_EncryptUpdate(ctx, cipherBuf[0].addr, outLen1.addr, plaintext.cstring, plaintext.len.cint) != 1:
      raise newException(ValueError, "EVP_EncryptUpdate failed")

    var outLen2: cint = 0
    if EVP_EncryptFinal_ex(ctx, cipherBuf[outLen1].addr, outLen2.addr) != 1:
      raise newException(ValueError, "EVP_EncryptFinal_ex failed")

    cipherBuf.setLen(outLen1 + outLen2)

    var rawCombined = "Salted__"
    for b in salt: rawCombined.add(char(b))
    rawCombined.add(cipherBuf)

    return encode(rawCombined)
  finally:
    EVP_CIPHER_CTX_free(ctx)

proc decryptAes*(ciphertext, secret: string, cfg: LocutusConfig = LocutusConfig()): string =
  var raw = ""
  try:
    raw = decode(ciphertext.strip())
  except Exception:
    raise newException(ValueError, "Decryption failed: invalid base64 encoding")

  if raw.len < 16 or not raw.startsWith("Salted__"):
    raise newException(ValueError, "Decryption failed: missing OpenSSL Salted__ header")

  var salt: array[8, uint8]
  for i in 0 ..< 8:
    salt[i] = uint8(raw[8 + i])

  var keyAndIv: array[48, uint8]
  let md = EVP_sha256()
  if PKCS5_PBKDF2_HMAC(secret.cstring, secret.len.cint, salt[0].addr, 8, 10000, md, 48, keyAndIv[0].addr) != 1:
    raise newException(ValueError, "PBKDF2 key derivation failed")

  let keyPtr = keyAndIv[0].addr
  let ivPtr = keyAndIv[32].addr

  let cipherData = raw[16..^1]
  let ctx = EVP_CIPHER_CTX_new()
  if ctx == nil:
    raise newException(ValueError, "Failed to create EVP_CIPHER_CTX")
  try:
    if EVP_DecryptInit_ex(ctx, EVP_aes_256_cbc(), nil, keyPtr, ivPtr) != 1:
      raise newException(ValueError, "EVP_DecryptInit_ex failed")

    var plainBuf = newString(cipherData.len + 32)
    var outLen1: cint = 0
    if EVP_DecryptUpdate(ctx, plainBuf[0].addr, outLen1.addr, cipherData.cstring, cipherData.len.cint) != 1:
      raise newException(ValueError, "EVP_DecryptUpdate failed")

    var outLen2: cint = 0
    if EVP_DecryptFinal_ex(ctx, plainBuf[outLen1].addr, outLen2.addr) != 1:
      raise newException(ValueError, "Decryption failed: bad key or corrupted ciphertext")

    plainBuf.setLen(outLen1 + outLen2)
    return plainBuf
  finally:
    EVP_CIPHER_CTX_free(ctx)

# Configuration Resolution (implemented in src/config.nim)
proc resolveConfig*(cli: CliOverrides = CliOverrides()): LocutusConfig =
  resolveFullConfig(cli)

# Redis Native Socket Execution with automatic fallback to redis-cli / docker
proc execRedis(redisUrl: string, cmdArgs: openArray[string]): (string, int) =
  execRedisAuto(redisUrl, cmdArgs)


proc runLuaScript*(redisUrl, scriptText, scriptSha: string, evalArgs: openArray[string]): string =
  var shaArgs: seq[string] = @["EVALSHA", scriptSha, "0"]
  for a in evalArgs:
    shaArgs.add(a)
  let (shaOut, exitCode1) = execRedis(redisUrl, shaArgs)
  if "NOSCRIPT" in shaOut:
    # Fallback to EVAL and automatically cache the script in Redis
    var evalCmd: seq[string] = @["EVAL", scriptText, "0"]
    for a in evalArgs:
      evalCmd.add(a)
    let (evalOut, exitCode2) = execRedis(redisUrl, evalCmd)
    if exitCode2 != 0:
      stderr.writeLine("Redis error: " & evalOut.strip())
      quit(exitCode2)
    return evalOut.strip()
  if exitCode1 != 0:
    stderr.writeLine("Redis error: " & shaOut.strip())
    quit(exitCode1)
  return shaOut.strip()

# Agent Identity Persistence
proc currentAgentPath*(): string =
  getHomeDir() / ".config" / "locutus" / "current_agent"

proc saveCurrentAgent*(name: string) =
  # 1. Save workspace-scoped .locutus.agent in current working directory
  try:
    let localFile = getCurrentDir() / ".locutus.agent"
    writeFile(localFile, name.strip() & "\n")
    secureFilePermissions(localFile)
  except OSError:
    discard

  # 2. Save user-scoped fallback file
  try:
    let p = currentAgentPath()
    createDir(p.splitPath.head)
    writeFile(p, name.strip() & "\n")
    secureFilePermissions(p)
  except OSError:
    discard

proc loadCurrentAgent*(): string =
  # 1. Check workspace-scoped .locutus.agent first
  try:
    let localFile = getCurrentDir() / ".locutus.agent"
    if fileExists(localFile):
      let val = readFile(localFile).strip()
      if val.len > 0:
        return val
  except OSError:
    discard

  # 2. Check user-scoped fallback file
  try:
    let p = currentAgentPath()
    if fileExists(p):
      return readFile(p).strip()
  except OSError:
    discard
  return ""

proc clearCurrentAgent*() =
  try:
    let localFile = getCurrentDir() / ".locutus.agent"
    if fileExists(localFile):
      removeFile(localFile)
  except OSError:
    discard
  try:
    let p = currentAgentPath()
    if fileExists(p):
      removeFile(p)
  except OSError:
    discard

proc getActiveAgentName*(cfg: LocutusConfig, explicitName: string = "", fallbackDefault: bool = false): string =
  if explicitName.len > 0:
    return explicitName
  if cfg.provenance.hasKey("agent_name") and cfg.provenance["agent_name"].source in {srcCli, srcEnv, srcCustomFile, srcWorkspaceFile, srcUserFile, srcSystemFile}:
    return cfg.agentName
  let envName = getEnv("LOCUTUS_AGENT_NAME", getEnv("A2A_NAME", getEnv("MY_NAME", "")))
  if envName.len > 0:
    return envName
  let saved = loadCurrentAgent()
  if saved.len > 0:
    return saved
  if fallbackDefault:
    if cfg.agentName.len > 0:
      return cfg.agentName
    return if cfg.project.len > 0: cfg.project & "-worker" else: "worker"
  return ""

# Core Operations
proc doRegister*(cfg: LocutusConfig, name, tags: string, ttl: int = -1): string =
  let effectiveTtl = if ttl > 0: ttl elif cfg.heartbeatTtl > 0: cfg.heartbeatTtl else: 150
  return runLuaScript(cfg.redisUrl, registerLua, registerSha, [cfg.prefix, name, tags, $effectiveTtl])

proc doDrain*(cfg: LocutusConfig, name: string, count: int = 50): string =
  return runLuaScript(cfg.redisUrl, drainLua, drainSha, [cfg.prefix, name, $count])

proc doUnregister*(cfg: LocutusConfig, name: string): string =
  let saved = loadCurrentAgent()
  if saved == name or name.len == 0:
    clearCurrentAgent()
  return runLuaScript(cfg.redisUrl, unregisterLua, unregisterSha, [cfg.prefix, name])

proc doTag*(cfg: LocutusConfig, name, action, tags: string): string =
  return runLuaScript(cfg.redisUrl, tagLua, tagSha, [cfg.prefix, name, action, tags])

proc cleanupOldTmpFiles*() =
  let tmpDir = getHomeDir() / ".config" / "locutus" / "tmp"
  if dirExists(tmpDir):
    let nowUnix = getTime().toUnix()
    for kind, path in walkDir(tmpDir):
      if kind == pcFile and path.endsWith(".tmp"):
        try:
          let info = getFileInfo(path)
          if nowUnix - info.lastWriteTime.toUnix() > 3600:
            removeFile(path)
        except OSError:
          discard

proc formatDirectory*(raw: string): string =
  if raw.strip().len == 0:
    return "No agents found."
  var lines = raw.strip().splitLines()
  var rows: seq[(string, string, string, string, string)] = @[]
  for line in lines:
    let parts = line.strip().split('|')
    if parts.len >= 3:
      let name = parts[0]
      let alive = if parts[1] == "1": "ACTIVE" else: "EXPIRED"
      let tags = parts[2]
      let state = if parts.len > 3 and parts[3].len > 0: parts[3].toUpperAscii else: "IDLE"
      let activity = if parts.len > 4: parts[4] else: ""
      rows.add((name, alive, state, tags, activity))
    elif line.strip().len > 0:
      rows.add((line.strip(), "", "", "", ""))

  if rows.len == 0:
    return "No agents found."

  result = "AGENT               STATUS     STATE      TAGS                ACTIVITY\n"
  result.add("------------------------------------------------------------------------------------\n")
  for (name, status, state, tags, activity) in rows:
    result.add(name.alignLeft(20) & status.alignLeft(11) & state.alignLeft(11) & tags.alignLeft(20) & activity & "\n")
  result = result.strip()

proc formatDirectoryJson*(raw: string): string =
  var list = newJArray()
  if raw.strip().len > 0:
    for line in raw.strip().splitLines():
      let parts = line.strip().split('|')
      if parts.len >= 3:
        var obj = newJObject()
        obj["agent"] = %parts[0]
        obj["status"] = %(if parts[1] == "1": "ACTIVE" else: "EXPIRED")
        var tagArr = newJArray()
        if parts[2].len > 0:
          for t in parts[2].split(','):
            let trimmed = t.strip()
            if trimmed.len > 0: tagArr.add(%trimmed)
        obj["tags"] = tagArr
        obj["state"] = %(if parts.len > 3 and parts[3].len > 0: parts[3].toUpperAscii else: "IDLE")
        obj["activity"] = %(if parts.len > 4: parts[4] else: "")
        list.add(obj)
  return $list

proc doDirectory*(cfg: LocutusConfig, filterTag: string = "", asJson: bool = false): string =
  let raw = runLuaScript(cfg.redisUrl, directoryLua, directorySha, [cfg.prefix, filterTag])
  if asJson:
    return formatDirectoryJson(raw)
  return formatDirectory(raw)

proc doOpen*(cfg: LocutusConfig, optName, optTags: string) =
  cleanupOldTmpFiles()
  randomize()
  var name = optName
  if name.len == 0:
    for attempt in 1..25:
      let candidate = cfg.project & "-worker-" & $rand(1000..9999)
      let (outStr, code) = execRedis(cfg.redisUrl, ["EXISTS", cfg.prefix & "heartbeat:" & candidate])
      if code == 0 and outStr.strip() == "0":
        name = candidate
        break
    if name.len == 0:
      name = cfg.project & "-worker-" & $rand(10000..99999)
  else:
    let (outStr, code) = execRedis(cfg.redisUrl, ["EXISTS", cfg.prefix & "heartbeat:" & name])
    if code == 0 and outStr.strip() == "1":
      echo "[NOTICE] Re-attaching to existing active agent '" & name & "'"
  let tags = if optTags.len > 0:
    if optTags.startsWith(cfg.project): optTags else: cfg.project & "," & optTags
  else:
    cfg.project

  saveCurrentAgent(name)
  discard doRegister(cfg, name, tags, cfg.heartbeatTtl)
  let backlog = doDrain(cfg, name, 50)

  echo "===================================================="
  echo "[LOCUTUS BUS] Registered Successfully"
  echo "- Agent Name : ", name
  echo "- Project    : ", cfg.project
  echo "- Tags       : ", tags
  echo "- Redis URL  : ", cfg.redisUrl, " (prefix: ", cfg.prefix, ")"
  echo "- Security   : HMAC-SHA256 authenticated (Air-Gap Prompt Firewall)"
  echo "- Engine     : Nim Native (EVALSHA cached)"
  echo "- Status     : Active & Listening on inbox"
  echo "===================================================="

  if backlog.len > 2 and backlog != "[]":
    echo "\n[PENDING BACKLOG]:"
    echo backlog

proc doListen*(cfg: LocutusConfig, name: string, timeoutSec: int = -1)

proc doSend*(cfg: LocutusConfig, toAgent, msgType, fromAgent, subject, body: string,
            tags: seq[string] = @[], replyTo: string = "", msgId: string = "", isBroadcast: bool = false,
            customTs: string = "", echoResult: bool = true, rearmListen: bool = false, listenTimeoutSec: int = -1): string =
  randomize()
  let secret = getSecret(cfg)
  let id = if msgId.len > 0: msgId else: "msg_" & $getTime().toUnix() & "_" & fromAgent & "_" & $rand(1000..9999)
  let ts = if customTs.len > 0: customTs else: now().utc.format("yyyy-MM-dd'T'HH:mm:ss'Z'")
  let finalBody = if cfg.encrypt: encryptAes(body, secret, cfg) else: body

  # Canonical concatenation for HMAC: id|from|to|type|subject|body|timestamp
  let canonical = id & "|" & fromAgent & "|" & toAgent & "|" & msgType & "|" & subject & "|" & finalBody & "|" & ts
  let sig = computeHmacSha256(secret, canonical)

  var node = newJObject()
  node["id"] = %id
  node["from"] = %fromAgent
  node["to"] = %toAgent
  node["type"] = %msgType
  if replyTo.len > 0:
    node["reply_to"] = %replyTo
  else:
    node["reply_to"] = newJNull()

  var tagArray = newJArray()
  for t in tags:
    tagArray.add(%t)
  if tagArray.len == 0 and cfg.project.len > 0:
    tagArray.add(%cfg.project)
  node["tags"] = tagArray

  node["subject"] = %subject
  node["body"] = %finalBody
  node["timestamp"] = %ts
  node["sig"] = %sig
  node["encrypted"] = %cfg.encrypt

  let msgJson = $node

  let effectiveTtl = if cfg.messageTtl > 0: cfg.messageTtl else: 604800
  let isTargetMulticast = isBroadcast or toAgent.startsWith("@") or toAgent == "*"
  var res = ""
  if isTargetMulticast:
    var target = ""
    if toAgent.startsWith("@"):
      let raw = toAgent[1..^1]
      if raw in ["*", "all", "@all"]: target = "*"
      elif raw.startsWith(cfg.project): target = raw
      else: target = cfg.project & "," & raw
    elif toAgent == "*":
      target = "*"
    elif isBroadcast:
      if tags.len > 0:
        if "*" in tags or "@all" in tags:
          target = "*"
        elif cfg.project in tags:
          target = tags.join(",")
        else:
          target = cfg.project & "," & tags.join(",")
      else:
        target = cfg.project
    else:
      target = if toAgent.len > 0: toAgent else: cfg.project

    res = runLuaScript(cfg.redisUrl, multicastLua, multicastSha, [cfg.prefix, target, msgJson, $effectiveTtl])
  else:
    res = runLuaScript(cfg.redisUrl, sendO2oLua, sendO2oSha, [cfg.prefix, toAgent, msgJson, $effectiveTtl])

  if echoResult and not rearmListen:
    echo res

  if rearmListen:
    let listenerAgent = if fromAgent.len > 0: fromAgent else: getActiveAgentName(cfg, "", fallbackDefault = true)
    if listenerAgent.len == 0:
      stderr.writeLine("Error: Cannot listen after send: no agent name identified.")
      quit(1)
    stderr.writeLine("[LOCUTUS BUS] Message sent to " & toAgent & ". Now listening on inbox for " & listenerAgent & "...")
    doListen(cfg, listenerAgent, listenTimeoutSec)

  return res


proc doListen*(cfg: LocutusConfig, name: string, timeoutSec: int = -1) =
  let secret = getSecret(cfg)
  let inboxKey = cfg.prefix & "inbox:" & name
  let isForever = (timeoutSec <= 0 and (timeoutSec == 0 or cfg.listenTimeout <= 0))
  let hbTtl = if cfg.heartbeatTtl > 0: cfg.heartbeatTtl else: 150
  let pollChunk = min(60, max(1, hbTtl div 2))

  # Initial Heartbeat & Directory Registration
  let (hbOut, hbCode) = execRedis(cfg.redisUrl, ["SET", cfg.prefix & "heartbeat:" & name, "1", "EX", $hbTtl])
  if hbCode != 0:
    stderr.writeLine("Redis error: " & hbOut.strip())
    quit(hbCode)
  discard execRedis(cfg.redisUrl, ["SADD", cfg.prefix & "active_agents", name])
  let (existingTags, _) = execRedis(cfg.redisUrl, ["HGET", cfg.prefix & "agent:" & name, "tags"])
  if existingTags.strip().len == 0 or existingTags.strip() == "(nil)":
    let projTag = if cfg.project.len > 0: cfg.project else: "default"
    discard execRedis(cfg.redisUrl, ["HSET", cfg.prefix & "agent:" & name, "tags", projTag])
    discard execRedis(cfg.redisUrl, ["SADD", cfg.prefix & "tag:" & projTag, name])

  let effectiveTimeout = if isForever: 0 elif timeoutSec > 0: timeoutSec else: cfg.listenTimeout
  let startTime = getTime().toUnix()
  var remaining = effectiveTimeout

  while isForever or remaining > 0:
    let waitSec = if isForever: pollChunk else: min(pollChunk, remaining)
    var (outStr, exitCode) = execRedis(cfg.redisUrl, ["--raw", "BRPOP", inboxKey, $waitSec])
    if exitCode != 0:
      stderr.writeLine("Redis error: " & outStr.strip())
      quit(exitCode)

    if outStr.strip().len == 0 or outStr.strip() == "(nil)":
      # Internal chunk timeout: renew heartbeat silently in Redis
      discard execRedis(cfg.redisUrl, ["SET", cfg.prefix & "heartbeat:" & name, "1", "EX", $hbTtl])
      discard execRedis(cfg.redisUrl, ["SADD", cfg.prefix & "active_agents", name])
      if not isForever:
        let elapsed = int(getTime().toUnix() - startTime)
        remaining = max(0, effectiveTimeout - elapsed)
        if remaining == 0:
          return # Silent zero-token exit
      continue

    let firstNl = outStr.find('\n')
    if firstNl < 0:
      continue

    let payloadStr = outStr[firstNl + 1 .. ^1].strip()

    var parsed: JsonNode
    try:
      parsed = parseJson(payloadStr)
    except JsonParsingError:
      stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping non-JSON payload from inbox")
      if not isForever:
        let elapsed = int(getTime().toUnix() - startTime)
        remaining = max(0, effectiveTimeout - elapsed)
      continue

    let id = parsed.getOrDefault("id").getStr("")
    let fromAgent = parsed.getOrDefault("from").getStr("")
    let toAgent = parsed.getOrDefault("to").getStr("")
    let msgType = parsed.getOrDefault("type").getStr("")
    let subject = parsed.getOrDefault("subject").getStr("")
    let body = parsed.getOrDefault("body").getStr("")
    let ts = parsed.getOrDefault("timestamp").getStr("")
    let sig = parsed.getOrDefault("sig").getStr("")
    let isEncrypted = parsed.getOrDefault("encrypted").getBool(false)

    # Validate HMAC
    let canonical = id & "|" & fromAgent & "|" & toAgent & "|" & msgType & "|" & subject & "|" & body & "|" & ts
    if not verifyHmac(secret, canonical, sig):
      stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping unauthenticated/tampered message (ID: " & id & ")")
      if not isForever:
        let elapsed = int(getTime().toUnix() - startTime)
        remaining = max(0, effectiveTimeout - elapsed)
      continue

    # Authenticated! Decrypt if required
    if isEncrypted:
      try:
        let decryptedBody = decryptAes(body, secret, cfg)
        parsed["body"] = %decryptedBody
        parsed["encrypted"] = %false
      except ValueError as e:
        stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping corrupted/undecryptable message: " & e.msg & " (ID: " & id & ")")
        if not isForever:
          let elapsed = int(getTime().toUnix() - startTime)
          remaining = max(0, effectiveTimeout - elapsed)
        continue

    echo $parsed
    return

proc doStatus*(cfg: LocutusConfig, name, state: string, activity: string = ""): string =
  let effectiveTtl = if cfg.heartbeatTtl > 0: cfg.heartbeatTtl else: 150
  return runLuaScript(cfg.redisUrl, statusLua, statusSha, [cfg.prefix, name, state.toLowerAscii, activity, $effectiveTtl])

proc doLock*(cfg: LocutusConfig, lockName: string, ttlSec: int = 30): (string, int) =
  let owner = getActiveAgentName(cfg, "")
  let res = runLuaScript(cfg.redisUrl, lockLua, lockSha, [cfg.prefix, lockName, owner, $ttlSec])
  if res == "1":
    return ("LOCKED " & lockName & " by " & owner, 0)
  else:
    return ("Error: Lock '" & lockName & "' is already held.", 1)

proc doUnlock*(cfg: LocutusConfig, lockName: string): (string, int) =
  let owner = getActiveAgentName(cfg, "")
  let res = runLuaScript(cfg.redisUrl, unlockLua, unlockSha, [cfg.prefix, lockName, owner])
  if res == "1":
    return ("UNLOCKED " & lockName, 0)
  else:
    return ("Error: Cannot unlock '" & lockName & "': not owner or lock not found.", 1)

proc doEnqueue*(cfg: LocutusConfig, queueName, msgType, fromAgent, subject, body: string,
                tags: seq[string] = @[], replyTo: string = "", msgId: string = "", customTs: string = ""): string =
  randomize()
  let secret = getSecret(cfg)
  let id = if msgId.len > 0: msgId else: "msg_" & $getTime().toUnix() & "_" & fromAgent & "_" & $rand(1000..9999)
  let ts = if customTs.len > 0: customTs else: now().utc.format("yyyy-MM-dd'T'HH:mm:ss'Z'")
  let finalBody = if cfg.encrypt: encryptAes(body, secret, cfg) else: body

  let canonical = id & "|" & fromAgent & "|queue:" & queueName & "|" & msgType & "|" & subject & "|" & finalBody & "|" & ts
  let sig = computeHmacSha256(secret, canonical)

  var node = newJObject()
  node["id"] = %id
  node["from"] = %fromAgent
  node["to"] = %("queue:" & queueName)
  node["type"] = %msgType
  if replyTo.len > 0:
    node["reply_to"] = %replyTo
  else:
    node["reply_to"] = newJNull()

  var tagArray = newJArray()
  for t in tags:
    tagArray.add(%t)
  if tagArray.len == 0 and cfg.project.len > 0:
    tagArray.add(%cfg.project)
  node["tags"] = tagArray

  node["subject"] = %subject
  node["body"] = %finalBody
  node["timestamp"] = %ts
  node["sig"] = %sig
  node["encrypted"] = %cfg.encrypt

  let msgJson = $node
  let effectiveTtl = if cfg.messageTtl > 0: cfg.messageTtl else: 604800
  discard runLuaScript(cfg.redisUrl, enqueueLua, enqueueSha, [cfg.prefix, queueName, msgJson, $effectiveTtl])
  return id

proc doWork*(cfg: LocutusConfig, queueName: string, timeoutSec: int = -1) =
  let secret = getSecret(cfg)
  let queueKey = cfg.prefix & "queue:" & queueName
  let isForever = (timeoutSec <= 0 and (timeoutSec == 0 or cfg.listenTimeout <= 0))
  let effectiveTimeout = if isForever: 0 elif timeoutSec > 0: timeoutSec else: (if cfg.listenTimeout > 0: cfg.listenTimeout else: 60)
  let startTime = getTime().toUnix()
  var remaining = effectiveTimeout

  while isForever or remaining > 0:
    let waitSec = if isForever: 60 else: remaining
    var (outStr, exitCode) = execRedis(cfg.redisUrl, ["--raw", "BRPOP", queueKey, $waitSec])
    if exitCode != 0:
      stderr.writeLine("Redis error: " & outStr.strip())
      quit(exitCode)
    if outStr.strip().len == 0 or outStr.strip() == "(nil)":
      if not isForever:
        let elapsed = int(getTime().toUnix() - startTime)
        remaining = max(0, effectiveTimeout - elapsed)
        if remaining == 0:
          return # Silent zero-token exit
      continue

    let firstNl = outStr.find('\n')
    if firstNl < 0:
      continue

    let payloadStr = outStr[firstNl + 1 .. ^1].strip()

    var parsed: JsonNode
    try:
      parsed = parseJson(payloadStr)
    except JsonParsingError:
      stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping non-JSON payload from queue")
      if not isForever:
        let elapsed = int(getTime().toUnix() - startTime)
        remaining = max(0, effectiveTimeout - elapsed)
      continue

    let id = parsed.getOrDefault("id").getStr("")
    let fromAgent = parsed.getOrDefault("from").getStr("")
    let toAgent = parsed.getOrDefault("to").getStr("")
    let msgType = parsed.getOrDefault("type").getStr("")
    let subject = parsed.getOrDefault("subject").getStr("")
    let body = parsed.getOrDefault("body").getStr("")
    let ts = parsed.getOrDefault("timestamp").getStr("")
    let sig = parsed.getOrDefault("sig").getStr("")
    let isEncrypted = parsed.getOrDefault("encrypted").getBool(false)

    # Validate HMAC
    let canonical = id & "|" & fromAgent & "|" & toAgent & "|" & msgType & "|" & subject & "|" & body & "|" & ts
    if not verifyHmac(secret, canonical, sig):
      stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping unauthenticated/tampered message (ID: " & id & ")")
      if not isForever:
        let elapsed = int(getTime().toUnix() - startTime)
        remaining = max(0, effectiveTimeout - elapsed)
      continue

    # Authenticated! Decrypt if required
    if isEncrypted:
      try:
        let decryptedBody = decryptAes(body, secret, cfg)
        parsed["body"] = %decryptedBody
        parsed["encrypted"] = %false
      except ValueError as e:
        stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping corrupted/undecryptable message: " & e.msg & " (ID: " & id & ")")
        if not isForever:
          let elapsed = int(getTime().toUnix() - startTime)
          remaining = max(0, effectiveTimeout - elapsed)
        continue

    echo $parsed
    return

proc doRequest*(cfg: LocutusConfig, toAgent, fromAgent, subject, body: string, timeoutSec: int = 30, rawOutput: bool = false) =
  randomize()
  let secret = getSecret(cfg)
  let reqId = "req_" & $getTime().toUnix() & "_" & fromAgent & "_" & $rand(1000..9999)
  let replyQueue = "reply:" & reqId
  let replyInboxKey = cfg.prefix & "inbox:" & replyQueue

  discard doSend(cfg, toAgent, "task", fromAgent, subject, body, tags = @[], replyTo = replyQueue, msgId = reqId, isBroadcast = false, echoResult = false)

  let startTime = getTime().toUnix()
  var remaining = timeoutSec

  while remaining > 0:
    var (outStr, exitCode) = execRedis(cfg.redisUrl, ["--raw", "BRPOP", replyInboxKey, $remaining])
    if exitCode != 0:
      stderr.writeLine("Redis error: " & outStr.strip())
      quit(exitCode)
    if outStr.strip().len == 0 or outStr.strip() == "(nil)":
      stderr.writeLine("Error: Request timed out waiting for reply from " & toAgent)
      quit(1)

    let firstNl = outStr.find('\n')
    if firstNl < 0:
      stderr.writeLine("Error: Request timed out waiting for reply from " & toAgent)
      quit(1)

    let payloadStr = outStr[firstNl + 1 .. ^1].strip()

    var parsed: JsonNode
    try:
      parsed = parseJson(payloadStr)
    except JsonParsingError:
      stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping non-JSON payload from reply inbox")
      let elapsed = int(getTime().toUnix() - startTime)
      remaining = max(0, timeoutSec - elapsed)
      continue

    let id = parsed.getOrDefault("id").getStr("")
    let sender = parsed.getOrDefault("from").getStr("")
    let toTarget = parsed.getOrDefault("to").getStr("")
    let msgType = parsed.getOrDefault("type").getStr("")
    let subj = parsed.getOrDefault("subject").getStr("")
    let bdy = parsed.getOrDefault("body").getStr("")
    let ts = parsed.getOrDefault("timestamp").getStr("")
    let sig = parsed.getOrDefault("sig").getStr("")
    let isEncrypted = parsed.getOrDefault("encrypted").getBool(false)

    # Validate HMAC
    let canonical = id & "|" & sender & "|" & toTarget & "|" & msgType & "|" & subj & "|" & bdy & "|" & ts
    if not verifyHmac(secret, canonical, sig):
      stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping unauthenticated/tampered reply (ID: " & id & ")")
      let elapsed = int(getTime().toUnix() - startTime)
      remaining = max(0, timeoutSec - elapsed)
      continue

    # Authenticated! Decrypt if required
    if isEncrypted:
      try:
        let decryptedBody = decryptAes(bdy, secret, cfg)
        parsed["body"] = %decryptedBody
        parsed["encrypted"] = %false
      except ValueError as e:
        stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping corrupted/undecryptable reply: " & e.msg & " (ID: " & id & ")")
        let elapsed = int(getTime().toUnix() - startTime)
        remaining = max(0, timeoutSec - elapsed)
        continue

    if rawOutput:
      echo parsed.getOrDefault("body").getStr("")
    else:
      echo $parsed
    return

  stderr.writeLine("Error: Request timed out waiting for reply from " & toAgent)
  quit(1)

proc doPub*(cfg: LocutusConfig, channel, message: string): string =
  let fullChan = cfg.prefix & "channel:" & channel
  let (res, code) = execRedis(cfg.redisUrl, ["PUBLISH", fullChan, message])
  if code != 0:
    stderr.writeLine("Redis error: " & res.strip())
    quit(code)
  return res.strip()

proc doSub*(cfg: LocutusConfig, channel: string, timeoutSec: int = -1) =
  let fullChan = cfg.prefix & "channel:" & channel
  let (res, code) = subscribeOne(cfg.redisUrl, fullChan, timeoutSec)
  if code != 0:
    stderr.writeLine(res)
    quit(code)
  if res.len > 0:
    echo res

# Main Entrypoint / CLI Router
proc main() =
  let rawArgs = commandLineParams()
  var cli: CliOverrides
  var positionalArgs: seq[string] = @[]

  var i = 0
  while i < rawArgs.len:
    let a = rawArgs[i]
    if a.startsWith("--profile="):
      cli.profile = a[10..^1]
    elif a == "--profile" and i + 1 < rawArgs.len:
      cli.profile = rawArgs[i+1]; inc i
    elif a.startsWith("--config="):
      cli.configFile = a[9..^1]
    elif a == "--config" and i + 1 < rawArgs.len:
      cli.configFile = rawArgs[i+1]; inc i
    elif a.startsWith("--redis-url="):
      cli.redisUrl = a[12..^1]
    elif (a == "--redis-url" or a == "-u") and i + 1 < rawArgs.len:
      cli.redisUrl = rawArgs[i+1]; inc i
    elif a.startsWith("-u="):
      cli.redisUrl = a[3..^1]
    elif a.startsWith("--prefix="):
      cli.prefix = a[9..^1]
    elif a == "--prefix" and i + 1 < rawArgs.len:
      cli.prefix = rawArgs[i+1]; inc i
    elif a.startsWith("--project="):
      cli.project = a[10..^1]
    elif a == "--project" and i + 1 < rawArgs.len:
      cli.project = rawArgs[i+1]; inc i
    elif a.startsWith("--agent-name="):
      cli.agentName = a[13..^1]
    elif a == "--agent-name" and i + 1 < rawArgs.len:
      cli.agentName = rawArgs[i+1]; inc i
    elif a.startsWith("--secret="):
      cli.secret = a[9..^1]
    elif a == "--secret" and i + 1 < rawArgs.len:
      cli.secret = rawArgs[i+1]; inc i
    elif a.startsWith("--secret-file="):
      cli.secretFile = a[14..^1]
    elif a == "--secret-file" and i + 1 < rawArgs.len:
      cli.secretFile = rawArgs[i+1]; inc i
    elif a == "--encrypt":
      cli.encrypt = some(true)
    elif a == "--no-encrypt":
      cli.encrypt = some(false)
    elif a == "--cluster":
      cli.cluster = some(true)
    elif a == "--no-cluster":
      cli.cluster = some(false)
    elif a.startsWith("--timeout="):
      try: cli.timeout = some(parseInt(a[10..^1]))
      except ValueError: discard
    elif a == "--timeout" and i + 1 < rawArgs.len:
      try: cli.timeout = some(parseInt(rawArgs[i+1]))
      except ValueError: discard
      inc i
    else:
      positionalArgs.add(a)
    inc i

  let cfg = resolveConfig(cli)
  var args = positionalArgs

  if "-v" in rawArgs or "--version" in rawArgs or (args.len > 0 and args[0].toLowerAscii == "version"):
    echo "locutus " & LocutusVersion
    return

  if args.len == 0 or args[0] in ["-h", "--help", "help"]:
    echo "Locutus " & LocutusVersion & " - High Performance Inter-Assistant Redis Bus (Nim Native)"
    echo "Usage:"
    echo "  locutus version"
    echo "  locutus open [name] [tags]"
    echo "  locutus listen [name] [timeout_sec]"
    echo "  locutus send --to <agent> [--type task|query|reply|status] --subject <subj> --body <body> [--listen/-l]"
    echo "  locutus reply --to <agent> --subject <subj> --body <body> [--reply-to <id>] [--listen/-l]"
    echo "  locutus broadcast [--tags <tags>] --subject <subj> --body <body>"
    echo "  locutus request --to <agent> --subject <subj> --body <body> [--timeout 30] [--raw]"
    echo "  locutus enqueue <queue_name> --subject <subj> --body <body>"
    echo "  locutus work <queue_name> [timeout_sec]"
    echo "  locutus status <idle|busy|error> [activity_text] [name]"
    echo "  locutus lock <lock_name> [ttl_sec]"
    echo "  locutus unlock <lock_name>"
    echo "  locutus pub <channel> <message>"
    echo "  locutus sub <channel> [timeout_sec]"
    echo "  locutus who [filter_tag]"
    echo "  locutus tag <add|remove|set> <tags> [name]"
    echo "  locutus drain [count] [name]"
    echo "  locutus close [name]"
    echo "  locutus get-secret"
    echo "  locutus config <show|get|path|init>"
    echo ""
    echo "Global Options:"
    echo "  --version, -v         Print version and exit"
    echo "  --profile <name>      Select configuration profile from config file"
    echo "  --config <file>       Explicit configuration file path"
    echo "  --redis-url, -u <url> Redis connection endpoint"
    echo "  --prefix <pfx>        Key namespace prefix"
    echo "  --project <proj>      Project isolation group"
    echo "  --encrypt             Enable AES-256-CBC payload encryption"
    echo "  --cluster             Enable Redis Cluster hash tag compatibility"
    return

  let subcmd = args[0].toLowerAscii
  case subcmd
  of "config":
    let action = if args.len > 1: args[1].toLowerAscii else: "show"
    case action
    of "show":
      let isJson = ("--json" in rawArgs) or ("-j" in rawArgs)
      if isJson:
        echo formatConfigJson(cfg)
      else:
        echo formatConfigTable(cfg)
    of "get":
      if args.len < 3:
        stderr.writeLine("Usage: locutus config get <key>")
        quit(1)
      let key = args[2].toLowerAscii.replace("-", "_")
      case key
      of "redis_url", "url": echo cfg.redisUrl
      of "prefix": echo cfg.prefix
      of "project": echo cfg.project
      of "agent_name", "agent": echo cfg.agentName
      of "secret": echo cfg.secret
      of "secret_file": echo cfg.secretFile
      of "encrypt": echo $cfg.encrypt
      of "cluster": echo $cfg.cluster
      of "heartbeat_ttl", "heartbeat": echo $cfg.heartbeatTtl
      of "message_ttl", "ttl": echo $cfg.messageTtl
      of "listen_timeout", "timeout": echo $cfg.listenTimeout
      of "profile": echo cfg.profile
      of "config_file", "config": echo cfg.activeConfigFile
      else:
        stderr.writeLine("Error: Unknown configuration key: " & key)
        quit(1)
    of "path", "paths":
      echo formatConfigPaths()
    of "init":
      let target = if args.len > 2: args[2] else: "workspace"
      echo initConfigFile(target)
    else:
      stderr.writeLine("Unknown config action: " & action)
      stderr.writeLine("Usage: locutus config <show|get|path|init>")
      quit(1)

  of "open", "register":
    let name = if args.len > 1: args[1] else: ""
    let tags = if args.len > 2: args[2] else: ""
    doOpen(cfg, name, tags)

  of "listen":
    var explicitName = ""
    var timeout = cfg.listenTimeout
    if args.len > 1:
      try:
        timeout = parseInt(args[1])
      except ValueError:
        explicitName = args[1]
    if args.len > 2:
      try:
        timeout = parseInt(args[2])
      except ValueError:
        discard
    let name = getActiveAgentName(cfg, explicitName, fallbackDefault = false)
    if name.len == 0:
      stderr.writeLine("Error: No agent name specified. Run 'locutus open <name>', pass the agent name ('locutus listen <name>'), or export LOCUTUS_AGENT_NAME=<name>.")
      quit(1)
    doListen(cfg, name, timeout)

  of "send", "broadcast", "reply":
    let isBroadcast = (subcmd == "broadcast")
    let isReply = (subcmd == "reply")
    var toAgent = ""
    var msgType = if isReply: "reply" else: "task"
    var fromAgent = getActiveAgentName(cfg, "")
    var subject = ""
    var body = ""
    var tags: seq[string] = @[]
    var replyTo = ""
    var msgId = ""
    var customTs = ""
    var rearmListen = false
    var listenTimeout = -1

    var i = 1
    while i < args.len:
      let a = args[i]
      if a.startsWith("--to="): toAgent = a[5..^1]
      elif a == "--to" and i + 1 < args.len: toAgent = args[i+1]; inc i
      elif a.startsWith("--type="): msgType = a[7..^1]
      elif a == "--type" and i + 1 < args.len: msgType = args[i+1]; inc i
      elif a.startsWith("--from="): fromAgent = a[7..^1]
      elif a == "--from" and i + 1 < args.len: fromAgent = args[i+1]; inc i
      elif a.startsWith("--subject="): subject = a[10..^1]
      elif a == "--subject" and i + 1 < args.len: subject = args[i+1]; inc i
      elif a.startsWith("--body="): body = a[7..^1]
      elif a == "--body" and i + 1 < args.len: body = args[i+1]; inc i
      elif a.startsWith("--tags="):
        for t in a[7..^1].split(','):
          if t.strip().len > 0: tags.add(t.strip())
      elif a == "--tags" and i + 1 < args.len:
        for t in args[i+1].split(','):
          if t.strip().len > 0: tags.add(t.strip())
        inc i
      elif a.startsWith("--reply-to="): replyTo = a[11..^1]
      elif a.startsWith("--reply_to="): replyTo = a[11..^1]
      elif (a == "--reply-to" or a == "--reply_to") and i + 1 < args.len: replyTo = args[i+1]; inc i
      elif a.startsWith("--id="): msgId = a[5..^1]
      elif a == "--id" and i + 1 < args.len: msgId = args[i+1]; inc i
      elif a.startsWith("--timestamp="): customTs = a[12..^1]
      elif a == "--timestamp" and i + 1 < args.len: customTs = args[i+1]; inc i
      elif a in ["--listen", "-l"]:
        rearmListen = true
      elif a.startsWith("--listen="):
        rearmListen = true
        try: listenTimeout = parseInt(a[9..^1]) except ValueError: discard
      elif a.startsWith("--listen-timeout="):
        rearmListen = true
        try: listenTimeout = parseInt(a[17..^1]) except ValueError: discard
      elif a == "--listen-timeout" and i + 1 < args.len:
        rearmListen = true
        try: listenTimeout = parseInt(args[i+1]) except ValueError: discard
        inc i
      elif not a.startsWith("-"):
        # Positional arguments fallback: <to> <subject> <body>
        if toAgent == "": toAgent = a
        elif subject == "": subject = a
        elif body == "": body = a
      inc i

    if isBroadcast and toAgent == "":
      if tags.len > 0:
        if "*" in tags or "@all" in tags:
          toAgent = "*"
        else:
          toAgent = "@" & tags.join(",")
      else:
        toAgent = if cfg.project.len > 0: "@" & cfg.project else: "*"

    if (not isBroadcast and toAgent.len == 0) or subject.len == 0 or body.len == 0:
      if not isBroadcast and toAgent.len == 0:
        stderr.writeLine("Error: Missing required argument '--to <recipient>'.")
        if isReply:
          stderr.writeLine("Usage: locutus reply --to <recipient> --subject <subj> --body <body> [--reply-to <id>] [--listen/-l]")
        else:
          stderr.writeLine("Usage: locutus send --to <recipient> --subject <subj> --body <body> [--listen/-l]")
      else:
        stderr.writeLine("Error: Missing required arguments. --subject and --body are required.")
        if isReply:
          stderr.writeLine("Usage: locutus reply --to <recipient> --subject <subj> --body <body> [--reply-to <id>] [--listen/-l]")
        elif not isBroadcast:
          stderr.writeLine("Usage: locutus send --to <recipient> --subject <subj> --body <body> [--listen/-l]")
        else:
          stderr.writeLine("Usage: locutus broadcast [--tags <tags>] --subject <subj> --body <body>")
      quit(1)

    discard doSend(cfg, toAgent, msgType, fromAgent, subject, body, tags, replyTo, msgId, isBroadcast, customTs, echoResult = true, rearmListen = rearmListen, listenTimeoutSec = listenTimeout)

  of "who":
    var filterTag = cfg.project
    var jsonOutput = false
    var showAll = false

    var i = 1
    while i < args.len:
      let a = args[i]
      if a in ["-a", "--all", "*", "@all"]:
        showAll = true
      elif a in ["--json", "-j"]:
        jsonOutput = true
      elif not a.startsWith("-"):
        filterTag = a
      inc i

    if showAll:
      filterTag = "*"
    echo doDirectory(cfg, filterTag, jsonOutput)

  of "tag":
    if args.len < 3:
      stderr.writeLine("Error: Missing arguments for tag command.")
      stderr.writeLine("Usage: locutus tag <add|remove|set> <tags> [name]")
      quit(1)
    let action = args[1].toLowerAscii
    if action notin ["add", "remove", "set"]:
      stderr.writeLine("Error: Invalid tag action '" & args[1] & "'. Expected add, remove, or set.")
      stderr.writeLine("Usage: locutus tag <add|remove|set> <tags> [name]")
      quit(1)
    let tags = args[2]
    let explicitName = if args.len > 3: args[3] else: ""
    let name = getActiveAgentName(cfg, explicitName, fallbackDefault = false)
    if name.len == 0:
      stderr.writeLine("Error: No agent name specified. Run 'locutus open <name>', pass the agent name, or export LOCUTUS_AGENT_NAME=<name>.")
      quit(1)
    echo doTag(cfg, name, action, tags)

  of "drain":
    var count = 50
    if args.len > 1:
      try:
        count = parseInt(args[1])
      except ValueError:
        stderr.writeLine("Error: Invalid count '" & args[1] & "' for drain command. Expected an integer.")
        stderr.writeLine("Usage: locutus drain [count] [name]")
        quit(1)
    let explicitName = if args.len > 2: args[2] else: ""
    let name = getActiveAgentName(cfg, explicitName, fallbackDefault = false)
    if name.len == 0:
      stderr.writeLine("Error: No agent name specified. Run 'locutus open <name>', pass the agent name, or export LOCUTUS_AGENT_NAME=<name>.")
      quit(1)
    echo doDrain(cfg, name, count)

  of "close", "unregister":
    let explicitName = if args.len > 1: args[1] else: ""
    let name = getActiveAgentName(cfg, explicitName, fallbackDefault = false)
    if name.len > 0:
      echo doUnregister(cfg, name)
    else:
      clearCurrentAgent()
      echo "OK"

  of "get-secret":
    echo getSecret(cfg)

  of "request":
    var toAgent = ""
    var fromAgent = getActiveAgentName(cfg, "")
    var subject = ""
    var body = ""
    var timeout = 30
    var rawOutput = false

    var i = 1
    while i < args.len:
      let a = args[i]
      if a.startsWith("--to="): toAgent = a[5..^1]
      elif a == "--to" and i + 1 < args.len: toAgent = args[i+1]; inc i
      elif a.startsWith("--from="): fromAgent = a[7..^1]
      elif a == "--from" and i + 1 < args.len: fromAgent = args[i+1]; inc i
      elif a.startsWith("--subject="): subject = a[10..^1]
      elif a == "--subject" and i + 1 < args.len: subject = args[i+1]; inc i
      elif a.startsWith("--body="): body = a[7..^1]
      elif a == "--body" and i + 1 < args.len: body = args[i+1]; inc i
      elif a.startsWith("--timeout="):
        try: timeout = parseInt(a[10..^1])
        except ValueError: discard
      elif a == "--timeout" and i + 1 < args.len:
        try: timeout = parseInt(args[i+1])
        except ValueError: discard
        inc i
      elif a in ["--raw", "-r"]:
        rawOutput = true
      elif not a.startsWith("-"):
        if toAgent == "": toAgent = a
        elif subject == "": subject = a
        elif body == "": body = a
      inc i

    if toAgent.len == 0 or subject.len == 0 or body.len == 0:
      stderr.writeLine("Error: Missing required arguments. --to, --subject, and --body are required.")
      stderr.writeLine("Usage: locutus request --to <agent> --subject <subj> --body <body> [--timeout 30] [--raw]")
      quit(1)

    doRequest(cfg, toAgent, fromAgent, subject, body, timeout, rawOutput)

  of "enqueue":
    if args.len < 2:
      stderr.writeLine("Error: Missing queue name.")
      stderr.writeLine("Usage: locutus enqueue <queue_name> --subject <subj> --body <body>")
      quit(1)
    let queueName = args[1]
    var msgType = "task"
    var fromAgent = getActiveAgentName(cfg, "")
    var subject = ""
    var body = ""
    var tags: seq[string] = @[]
    var replyTo = ""
    var msgId = ""
    var customTs = ""

    var i = 2
    while i < args.len:
      let a = args[i]
      if a.startsWith("--type="): msgType = a[7..^1]
      elif a == "--type" and i + 1 < args.len: msgType = args[i+1]; inc i
      elif a.startsWith("--from="): fromAgent = a[7..^1]
      elif a == "--from" and i + 1 < args.len: fromAgent = args[i+1]; inc i
      elif a.startsWith("--subject="): subject = a[10..^1]
      elif a == "--subject" and i + 1 < args.len: subject = args[i+1]; inc i
      elif a.startsWith("--body="): body = a[7..^1]
      elif a == "--body" and i + 1 < args.len: body = args[i+1]; inc i
      elif a.startsWith("--tags="):
        for t in a[7..^1].split(','):
          if t.strip().len > 0: tags.add(t.strip())
      elif a == "--tags" and i + 1 < args.len:
        for t in args[i+1].split(','):
          if t.strip().len > 0: tags.add(t.strip())
        inc i
      elif a.startsWith("--reply-to=") or a.startsWith("--reply_to="): replyTo = a[11..^1]
      elif (a == "--reply-to" or a == "--reply_to") and i + 1 < args.len: replyTo = args[i+1]; inc i
      elif a.startsWith("--id="): msgId = a[5..^1]
      elif a == "--id" and i + 1 < args.len: msgId = args[i+1]; inc i
      elif a.startsWith("--timestamp="): customTs = a[12..^1]
      elif a == "--timestamp" and i + 1 < args.len: customTs = args[i+1]; inc i
      elif not a.startsWith("-"):
        if subject == "": subject = a
        elif body == "": body = a
      inc i

    if subject.len == 0 or body.len == 0:
      stderr.writeLine("Error: Missing required arguments. --subject and --body are required.")
      stderr.writeLine("Usage: locutus enqueue <queue_name> --subject <subj> --body <body>")
      quit(1)

    let id = doEnqueue(cfg, queueName, msgType, fromAgent, subject, body, tags, replyTo, msgId, customTs)
    echo id

  of "work":
    if args.len < 2:
      stderr.writeLine("Error: Missing queue name.")
      stderr.writeLine("Usage: locutus work <queue_name> [timeout_sec]")
      quit(1)
    let queueName = args[1]
    var timeout = -1
    if args.len > 2:
      try: timeout = parseInt(args[2])
      except ValueError: discard
    doWork(cfg, queueName, timeout)

  of "status":
    if args.len < 2:
      stderr.writeLine("Error: Missing state argument for status command.")
      stderr.writeLine("Usage: locutus status <idle|busy|error> [activity_text] [name]")
      quit(1)
    let state = args[1]
    let activity = if args.len > 2: args[2] else: ""
    let explicitName = if args.len > 3: args[3] else: ""
    let name = getActiveAgentName(cfg, explicitName, fallbackDefault = false)
    if name.len == 0:
      stderr.writeLine("Error: No agent name specified. Run 'locutus open <name>', pass the agent name, or export LOCUTUS_AGENT_NAME=<name>.")
      quit(1)
    echo doStatus(cfg, name, state, activity)

  of "lock":
    if args.len < 2:
      stderr.writeLine("Error: Missing lock name.")
      stderr.writeLine("Usage: locutus lock <lock_name> [ttl_sec]")
      quit(1)
    let lockName = args[1]
    var ttl = 30
    if args.len > 2:
      try: ttl = parseInt(args[2])
      except ValueError: discard
    let (msg, code) = doLock(cfg, lockName, ttl)
    if code != 0:
      stderr.writeLine(msg)
      quit(code)
    echo msg

  of "unlock":
    if args.len < 2:
      stderr.writeLine("Error: Missing lock name.")
      stderr.writeLine("Usage: locutus unlock <lock_name>")
      quit(1)
    let lockName = args[1]
    let (msg, code) = doUnlock(cfg, lockName)
    if code != 0:
      stderr.writeLine(msg)
      quit(code)
    echo msg

  of "pub", "publish":
    if args.len < 3:
      stderr.writeLine("Error: Missing arguments for pub command.")
      stderr.writeLine("Usage: locutus pub <channel> <message>")
      quit(1)
    let channel = args[1]
    let message = args[2]
    echo doPub(cfg, channel, message)

  of "sub", "subscribe":
    if args.len < 2:
      stderr.writeLine("Error: Missing channel name for sub command.")
      stderr.writeLine("Usage: locutus sub <channel> [timeout_sec]")
      quit(1)
    let channel = args[1]
    var timeout = -1
    if args.len > 2:
      try: timeout = parseInt(args[2])
      except ValueError: discard
    doSub(cfg, channel, timeout)

  else:
    stderr.writeLine("Unknown subcommand: " & subcmd)
    quit(1)

when isMainModule:
  main()
