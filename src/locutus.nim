# src/locutus.nim
# High-performance, single-binary inter-assistant communication bus over Redis.
# Embeds Lua scripts at compile time and utilizes EVALSHA caching with automatic EVAL fallback.

import std/[
  os, strutils, json, openssl, sha1,
  times, random, options, base64, tables, sets, nativesockets
]
when defined(posix):
  import posix
elif defined(windows):
  import std/osproc
import config, redis, std/[net, asyncdispatch]

proc isPidAlive*(pid: int): bool =
  if pid <= 0: return false
  when defined(posix):
    if kill(Pid(pid), 0) == 0:
      return true
    return errno == EPERM
  elif defined(windows):
    let (outp, code) = execCmdEx("tasklist /FI \"PID eq " & $pid & "\" /NH")
    return code == 0 and $pid in outp
  else:
    return true

proc getHostNameStr*(): string =
  try:
    let h = getHostName()
    if h.len > 0: return h
  except Exception:
    discard
  return getEnv("HOSTNAME", getEnv("COMPUTERNAME", "localhost"))

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
  scatterLua*    = staticRead("../scripts/scatter.lua")
  claimLua*      = staticRead("../scripts/claim.lua")
  claimRenewLua* = staticRead("../scripts/claim_renew.lua")
  ackLua*        = staticRead("../scripts/ack.lua")
  blackboardLua* = staticRead("../scripts/blackboard.lua")
  floorLua*      = staticRead("../scripts/floor.lua")
  cancelLua*     = staticRead("../scripts/cancel.lua")
  ballotLua*     = staticRead("../scripts/ballot.lua")
  leaderLua*     = staticRead("../scripts/leader.lua")
  workflowLua*   = staticRead("../scripts/workflow.lua")
  sweepLua*      = staticRead("../scripts/sweep.lua")
  LocutusVersion* = "0.1.2"

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
  scatterSha*    = computeSha1(scatterLua)
  claimSha*      = computeSha1(claimLua)
  claimRenewSha* = computeSha1(claimRenewLua)
  ackSha*        = computeSha1(ackLua)
  blackboardSha* = computeSha1(blackboardLua)
  floorSha*      = computeSha1(floorLua)
  cancelSha*     = computeSha1(cancelLua)
  ballotSha*     = computeSha1(ballotLua)
  leaderSha*     = computeSha1(leaderLua)
  workflowSha*   = computeSha1(workflowLua)
  sweepSha*      = computeSha1(sweepLua)


proc secureFilePermissions*(path: string) =
  when not defined(windows):
    try:
      setFilePermissions(path, {fpUserRead, fpUserWrite})
    except CatchableError:
      discard

proc parseRequiredInt*(val, flagName: string): int =
  try:
    return parseInt(val)
  except ValueError:
    stderr.writeLine("Error: Invalid integer for " & flagName & ": '" & val & "'")
    quit(1)


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
  let hmacRes = HMAC(mdPtr, secret.cstring, secret.len.cint, data.cstring, data.len.csize_t, md[0].addr, mdLen.addr)
  if hmacRes == nil:
    raise newException(ValueError, "OpenSSL HMAC-SHA256 computation failed")
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

proc formatRedisValue*(val: RedisValue, cmd: string = ""): string =
  case val.kind
  of vkNil:
    "(nil)"
  of vkStatus, vkString:
    val.strVal
  of vkInteger:
    $val.intVal
  of vkList:
    if cmd.toUpperAscii in ["BRPOP", "BLPOP"] and val.listVal.len >= 2:
      return formatRedisValue(val.listVal[0]) & "\n" & formatRedisValue(val.listVal[1])
    var parts: seq[string] = @[]
    for item in val.listVal:
      parts.add(formatRedisValue(item))
    parts.join("\n")

proc openRedisClient*(redisUrl: string): Redis =
  let parsed = parseRedisUrl(redisUrl)
  result = open(parsed.host, parsed.port.Port)
  if parsed.password.len > 0:
    result.auth(parsed.password)
  if parsed.db != 0:
    discard result.select(parsed.db)

proc connectRedis*(redisUrl: string): Redis =
  try:
    result = openRedisClient(redisUrl)
  except CatchableError as e:
    stderr.writeLine("Redis error: Could not connect to Redis: " & e.msg)
    quit(1)

proc reconnectRedisClientMs*(redisUrl: string, client: var Redis, remainingMs: var int, isForever: bool): bool =
  ## Loops with exponential backoff and full jitter until connection is restored
  ## or until a finite timeout is exhausted. Returns true on successful reconnect,
  ## or false if timeout expired while disconnected.
  if client != nil:
    try: client.close() except CatchableError: discard
    client = nil

  var attempt = 0
  let baseMs = 100
  let capMs = 5000
  let startEpoch = epochTime()
  let initialRemaining = remainingMs

  while isForever or remainingMs > 0:
    let factor = 1 shl min(attempt, 6) # 1, 2, 4, 8, 16, 32, 64
    let maxInterval = min(capMs, baseMs * factor)
    let sleepMs = if maxInterval > 0: rand(maxInterval) else: baseMs

    if not isForever:
      let elapsedMs = int((epochTime() - startEpoch) * 1000)
      remainingMs = max(0, initialRemaining - elapsedMs)
      if remainingMs <= 0:
        return false

    stderr.writeLine("[LOCUTUS] Connection severed. Reconnecting (attempt " & $(attempt + 1) & ") in " & $sleepMs & "ms...")
    sleep(sleepMs)

    if not isForever:
      let elapsedMs = int((epochTime() - startEpoch) * 1000)
      remainingMs = max(0, initialRemaining - elapsedMs)
      if remainingMs <= 0:
        return false

    try:
      client = openRedisClient(redisUrl)
      stderr.writeLine("[LOCUTUS] Connection re-established successfully.")
      return true
    except CatchableError:
      attempt += 1

  return false

proc reconnectRedisClient*(redisUrl: string, client: var Redis, remainingSec: var int, isForever: bool): bool =
  var ms = if isForever: 0 else: remainingSec * 1000
  result = reconnectRedisClientMs(redisUrl, client, ms, isForever)
  if not isForever:
    remainingSec = (ms + 999) div 1000

# Graceful Signal Trapping and Resource Cleanup (TASK-18)
type
  ActiveCleanup = object
    url: string
    key: string

var activeCleanups: seq[ActiveCleanup] = @[]
var isCleaningUp = false

proc registerCleanup*(url, key: string) =
  activeCleanups.add(ActiveCleanup(url: url, key: key))

proc unregisterCleanup*(key: string) =
  for i in countdown(activeCleanups.len - 1, 0):
    if activeCleanups[i].key == key:
      activeCleanups.delete(i)

proc runSignalCleanups*() =
  if isCleaningUp: return
  isCleaningUp = true
  for c in activeCleanups:
    try:
      var client = openRedisClient(c.url)
      defer: (try: client.close() except CatchableError: discard)
      discard client.del(@[c.key])
    except Exception:
      discard

when defined(posix):
  proc handleSignal(sig: cint) {.noconv.} =
    runSignalCleanups()
    quit(128 + int(sig))

  proc installSignalHandlers*() =
    var sa: Sigaction
    sa.sa_handler = handleSignal
    discard sigemptyset(sa.sa_mask)
    sa.sa_flags = 0
    discard sigaction(SIGINT, sa)
    discard sigaction(SIGTERM, sa)
    setControlCHook(proc() {.noconv.} =
      runSignalCleanups()
      quit(130)
    )
else:
  proc installSignalHandlers*() =
    setControlCHook(proc() {.noconv.} =
      runSignalCleanups()
      quit(130)
    )

proc runLuaScript*(redisUrl, scriptText, scriptSha: string, evalArgs: openArray[string]): string =
  var client = connectRedis(redisUrl)
  defer:
    try: client.close() except CatchableError: discard

  var argSeq: seq[string] = @[]
  for a in evalArgs:
    argSeq.add(a)

  try:
    let resp = client.evalSha(scriptSha, @[], argSeq)
    return formatRedisValue(resp)
  except RedisError as e:
    if "NOSCRIPT" in e.msg:
      try:
        let evalResp = client.eval(scriptText, @[], argSeq)
        return formatRedisValue(evalResp)
      except CatchableError as e2:
        stderr.writeLine("Redis error: " & e2.msg)
        quit(1)
    else:
      stderr.writeLine("Redis error: " & e.msg)
      quit(1)
  except CatchableError as e:
    stderr.writeLine("Redis error: " & e.msg)
    quit(1)

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

proc getActiveListenerInfo*(cfg: LocutusConfig, name: string): tuple[active: bool, pid: int, host: string] =
  var client: Redis
  try:
    client = openRedisClient(cfg.redisUrl)
  except CatchableError:
    return (false, 0, "")
  defer:
    try: client.close() except CatchableError: discard

  try:
    let val = client.get(cfg.prefix & "listener:" & name)
    if val == redisNil or val.len == 0:
      return (false, 0, "")
    let node = parseJson(val)
    let pid = node.getOrDefault("pid").getInt(0)
    let host = node.getOrDefault("host").getStr("")
    let currentHost = getHostNameStr()
    if host == currentHost and pid > 0:
      if not isPidAlive(pid):
        # Stale lock: process is no longer alive on this machine
        discard client.del(@[cfg.prefix & "listener:" & name])
        return (false, 0, "")
      else:
        return (true, pid, host)
    else:
      return (true, pid, host)
  except CatchableError:
    return (false, 0, "")

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
  try:
    var client = openRedisClient(cfg.redisUrl)
    defer: (try: client.close() except CatchableError: discard)
    discard client.del(@[cfg.prefix & "listener:" & name])
  except CatchableError:
    discard
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
      let activity = if parts.len > 4: parts[4..^1].join("|") else: ""
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
        obj["activity"] = %(if parts.len > 4: parts[4..^1].join("|") else: "")
        list.add(obj)
  return $list

proc doDirectory*(cfg: LocutusConfig, filterTag: string = "", asJson: bool = false): string =
  let raw = runLuaScript(cfg.redisUrl, directoryLua, directorySha, [cfg.prefix, filterTag])
  if asJson:
    return formatDirectoryJson(raw)
  return formatDirectory(raw)

proc doListen*(cfg: LocutusConfig, name: string, timeoutSec: int = -1)

proc doOpen*(cfg: LocutusConfig, optName, optTags: string, rearmListen: bool = false, listenTimeoutSec: int = -1) =
  cleanupOldTmpFiles()
  randomize()
  var client = connectRedis(cfg.redisUrl)
  defer:
    try: client.close() except CatchableError: discard

  var name = optName
  if name.len == 0:
    for attempt in 1..25:
      let candidate = cfg.project & "-worker-" & $rand(1000..9999)
      try:
        if not client.exists(cfg.prefix & "heartbeat:" & candidate):
          name = candidate
          break
      except CatchableError:
        discard
    if name.len == 0:
      name = cfg.project & "-worker-" & $rand(10000..99999)
  else:
    try:
      if client.exists(cfg.prefix & "heartbeat:" & name):
        echo "[NOTICE] Re-attaching to existing active agent '" & name & "'"
    except CatchableError:
      discard
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

  if rearmListen:
    let (alreadyListening, existingPid, existingHost) = getActiveListenerInfo(cfg, name)
    if alreadyListening:
      stderr.writeLine("[LOCUTUS LISTENER] Listener already active for agent '" & name & "' (PID " & $existingPid & " on " & existingHost & "). Skipping duplicate listener.")
    else:
      stderr.writeLine("[LOCUTUS LISTENER] Entering listening mode for agent '" & name & "'...")
      doListen(cfg, name, listenTimeoutSec)

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
    let destQueue = if msgType == "reply" and (replyTo.startsWith("scatter:") or replyTo.startsWith("reply:")): replyTo else: toAgent
    res = runLuaScript(cfg.redisUrl, sendO2oLua, sendO2oSha, [cfg.prefix, destQueue, msgJson, $effectiveTtl])

  if echoResult and not rearmListen:
    echo res

  if rearmListen:
    let listenerAgent = if fromAgent.len > 0: fromAgent else: getActiveAgentName(cfg, "", fallbackDefault = true)
    if listenerAgent.len == 0:
      stderr.writeLine("Error: Cannot listen after send: no agent name identified.")
      quit(1)
    let (alreadyListening, existingPid, existingHost) = getActiveListenerInfo(cfg, listenerAgent)
    if alreadyListening:
      stderr.writeLine("[LOCUTUS BUS] Message sent to " & toAgent & ". Active listener already running for " & listenerAgent & " (PID " & $existingPid & " on " & existingHost & "); skipping duplicate listener.")
      return res
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
  var client = connectRedis(cfg.redisUrl)
  defer:
    try: client.close() except CatchableError: discard

  try:
    discard client.setEx(cfg.prefix & "heartbeat:" & name, hbTtl, "1")
  except CatchableError as e:
    stderr.writeLine("Redis error: " & e.msg)
    quit(1)

  try:
    discard client.sadd(cfg.prefix & "active_agents", name)
    let existingTags = client.hGet(cfg.prefix & "agent:" & name, "tags")
    if existingTags == redisNil or existingTags.len == 0:
      let projTag = if cfg.project.len > 0: cfg.project else: "default"
      discard client.hSet(cfg.prefix & "agent:" & name, "tags", projTag)
      discard client.sadd(cfg.prefix & "tag:" & projTag, name)
  except CatchableError as e:
    stderr.writeLine("Redis error: " & e.msg)
    quit(1)

  # Register listener ownership in Redis
  let myPid = getCurrentProcessId()
  let myHost = getHostNameStr()
  var listenerNode = newJObject()
  listenerNode["pid"] = %myPid
  listenerNode["host"] = %myHost
  listenerNode["started"] = %(getTime().toUnix())
  let listenerJson = $listenerNode
  let listenerKey = cfg.prefix & "listener:" & name
  try:
    discard client.setEx(listenerKey, hbTtl, listenerJson)
  except CatchableError as e:
    stderr.writeLine("Redis error: " & e.msg)
    quit(1)
  registerCleanup(cfg.redisUrl, listenerKey)

  let effectiveTimeout = if isForever: 0 elif timeoutSec > 0: timeoutSec else: cfg.listenTimeout
  let startTime = getTime().toUnix()
  var remaining = effectiveTimeout

  try:
    while isForever or remaining > 0:
      let waitSec = if isForever: pollChunk else: min(pollChunk, remaining)
      var popRes: RedisList
      try:
        popRes = client.bRPop(@[inboxKey], waitSec)
      except CatchableError as e:
        if not reconnectRedisClient(cfg.redisUrl, client, remaining, isForever):
          return # Timeout expired while disconnected
        # Re-register heartbeat & listener lock on new connection
        try:
          discard client.setEx(cfg.prefix & "heartbeat:" & name, hbTtl, "1")
          discard client.sadd(cfg.prefix & "active_agents", name)
          discard client.setEx(listenerKey, hbTtl, listenerJson)
        except CatchableError:
          discard
        continue

      if popRes.len == 0:
        if not isForever:
          let elapsed = int(getTime().toUnix() - startTime)
          remaining = max(0, effectiveTimeout - elapsed)
          if remaining == 0:
            return # Silent zero-token exit

        # Internal chunk timeout: renew heartbeat & listener lock silently in Redis only if still owner
        try:
          discard client.setEx(cfg.prefix & "heartbeat:" & name, hbTtl, "1")
          discard client.sadd(cfg.prefix & "active_agents", name)
          let currentVal = client.get(cfg.prefix & "listener:" & name)
          var isMyLock = false
          if currentVal == redisNil or currentVal.len == 0:
            isMyLock = true
          else:
            try:
              let node = parseJson(currentVal)
              if node.getOrDefault("pid").getInt(0) == myPid and node.getOrDefault("host").getStr("") == myHost:
                isMyLock = true
            except Exception:
              discard
          if isMyLock:
            discard client.setEx(cfg.prefix & "listener:" & name, hbTtl, listenerJson)
        except CatchableError:
          discard
        continue

      if popRes.len < 2:
        continue

      let payloadStr = popRes[1].strip()

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
  finally:
    unregisterCleanup(listenerKey)
    try:
      let currentVal = client.get(listenerKey)
      if currentVal != redisNil and currentVal.len > 0:
        let node = parseJson(currentVal)
        if node.getOrDefault("pid").getInt(0) == myPid and node.getOrDefault("host").getStr("") == myHost:
          discard client.del(@[listenerKey])
    except Exception:
      discard

proc doStatus*(cfg: LocutusConfig, name, state: string, activity: string = ""): string =
  let effectiveTtl = if cfg.heartbeatTtl > 0: cfg.heartbeatTtl else: 150
  return runLuaScript(cfg.redisUrl, statusLua, statusSha, [cfg.prefix, name, state.toLowerAscii, activity, $effectiveTtl])

proc doLock*(cfg: LocutusConfig, lockName: string, ttlSec: int = 30, withFencing: bool = false, rawOutput: bool = false): (string, int) =
  let owner = getActiveAgentName(cfg, "")
  let fencingArg = if withFencing: "1" else: "0"
  let res = runLuaScript(cfg.redisUrl, lockLua, lockSha, [cfg.prefix, lockName, owner, $ttlSec, fencingArg])
  if res != "0" and not res.startsWith("ERR:"):
    if withFencing:
      let token = res
      if rawOutput:
        return (token, 0)
      else:
        return ("LOCKED " & lockName & " by " & owner & " (fencing: " & token & ")", 0)
    else:
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

proc isRunCancelled*(client: Redis, cfg: LocutusConfig, runId: string): bool =
  if runId.len == 0 or client == nil: return false
  try:
    let res = client.get(cfg.prefix & "cancel:" & runId)
    if res != redisNil and res.len > 0:
      let parsed = parseJson(res)
      let secret = getSecret(cfg)
      let reason = parsed.getOrDefault("reason").getStr("")
      let byAgent = parsed.getOrDefault("by").getStr("")
      let ts = parsed.getOrDefault("timestamp").getStr("")
      let sig = parsed.getOrDefault("sig").getStr("")
      let canonical = runId & "|" & reason & "|" & byAgent & "|" & ts
      if sig.len > 0 and verifyHmac(secret, canonical, sig):
        return true
  except CatchableError:
    discard
  return false

proc isRunCancelled*(cfg: LocutusConfig, runId: string): bool =
  if runId.len == 0: return false
  var client: Redis
  try:
    client = openRedisClient(cfg.redisUrl)
  except CatchableError:
    return false
  defer:
    try: client.close() except CatchableError: discard
  return isRunCancelled(client, cfg, runId)

proc doWork*(cfg: LocutusConfig, queueName: string, timeoutSec: int = -1, runId: string = "") =
  let secret = getSecret(cfg)
  let queueKey = if queueName.startsWith("dlq:"):
                   cfg.prefix & "queue:dlq:{" & queueName[4..^1] & "}"
                 else:
                   cfg.prefix & "queue:{" & queueName & "}"
  let isForever = (timeoutSec <= 0 and (timeoutSec == 0 or cfg.listenTimeout <= 0))
  let effectiveTimeout = if isForever: 0 elif timeoutSec > 0: timeoutSec else: (if cfg.listenTimeout > 0: cfg.listenTimeout else: 60)
  let startTime = getTime().toUnix()
  var remaining = effectiveTimeout

  let workerName = getActiveAgentName(cfg, "")
  let hbTtl = if cfg.heartbeatTtl > 0: cfg.heartbeatTtl else: 150
  let pollChunk = min(60, max(1, hbTtl div 2))

  var client = connectRedis(cfg.redisUrl)
  defer:
    try: client.close() except CatchableError: discard

  if workerName.len > 0:
    try:
      discard client.setEx(cfg.prefix & "heartbeat:" & workerName, hbTtl, "1")
      discard client.sadd(cfg.prefix & "active_agents", workerName)
    except CatchableError as e:
      stderr.writeLine("Redis error: " & e.msg)
      quit(1)

  while isForever or remaining > 0:
    if runId.len > 0 and isRunCancelled(client, cfg, runId):
      stderr.writeLine("Run " & runId & " was cancelled. Worker exiting.")
      return
    let waitSec = if isForever: pollChunk else: min(pollChunk, remaining)
    var popRes: RedisList
    try:
      popRes = client.bRPop(@[queueKey], waitSec)
    except CatchableError as e:
      if not reconnectRedisClient(cfg.redisUrl, client, remaining, isForever):
        return # Timeout expired while disconnected
      if workerName.len > 0:
        try:
          discard client.setEx(cfg.prefix & "heartbeat:" & workerName, hbTtl, "1")
          discard client.sadd(cfg.prefix & "active_agents", workerName)
        except CatchableError:
          discard
      continue
    if popRes.len == 0:
      if workerName.len > 0:
        try:
          discard client.setEx(cfg.prefix & "heartbeat:" & workerName, hbTtl, "1")
          discard client.sadd(cfg.prefix & "active_agents", workerName)
        except CatchableError:
          discard
      if not isForever:
        let elapsed = int(getTime().toUnix() - startTime)
        remaining = max(0, effectiveTimeout - elapsed)
        if remaining == 0:
          return # Silent zero-token exit
      continue

    if popRes.len < 2:
      continue

    let payloadStr = popRes[1].strip()

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

proc doAck*(cfg: LocutusConfig, queueName, taskId: string): int

proc doClaim*(cfg: LocutusConfig, queueName: string, timeoutSec: int = -1, leaseSec: int = 120, rawOutput: bool = false, runId: string = "") =
  let secret = getSecret(cfg)
  let workerName = getActiveAgentName(cfg, "")
  let isForever = (timeoutSec <= 0 and (timeoutSec == 0 or cfg.listenTimeout <= 0))
  let effectiveTimeout = if isForever: 0 elif timeoutSec > 0: timeoutSec else: (if cfg.listenTimeout > 0: cfg.listenTimeout else: 60)
  let startTime = epochTime()
  var remainingMs = effectiveTimeout * 1000

  let hbTtl = if cfg.heartbeatTtl > 0: cfg.heartbeatTtl else: 150

  var client = connectRedis(cfg.redisUrl)
  defer:
    try: client.close() except CatchableError: discard

  if workerName.len > 0:
    try:
      discard client.setEx(cfg.prefix & "heartbeat:" & workerName, hbTtl, "1")
      discard client.sadd(cfg.prefix & "active_agents", workerName)
    except CatchableError as e:
      stderr.writeLine("Redis error: " & e.msg)
      quit(1)

  var backoffMs = 250

  while isForever or remainingMs > 0 or effectiveTimeout == 0:
    if runId.len > 0 and isRunCancelled(client, cfg, runId):
      stderr.writeLine("Run " & runId & " was cancelled. Worker exiting.")
      return

    var res = ""
    var exitCode = 0
    try:
      let val = client.evalSha(claimSha, @[], @[cfg.prefix, queueName, workerName, $leaseSec, "3"])
      res = formatRedisValue(val)
    except RedisError as e:
      if "NOSCRIPT" in e.msg:
        try:
          let val = client.eval(claimLua, @[], @[cfg.prefix, queueName, workerName, $leaseSec, "3"])
          res = formatRedisValue(val)
        except CatchableError as e2:
          if "Server closed connection" in e2.msg or "recv failed" in e2.msg:
            if not reconnectRedisClientMs(cfg.redisUrl, client, remainingMs, isForever):
              return
            continue
          res = e2.msg; exitCode = 1
      elif "Server closed connection" in e.msg or "recv failed" in e.msg:
        if not reconnectRedisClientMs(cfg.redisUrl, client, remainingMs, isForever):
          return
        if workerName.len > 0:
          try:
            discard client.setEx(cfg.prefix & "heartbeat:" & workerName, hbTtl, "1")
            discard client.sadd(cfg.prefix & "active_agents", workerName)
          except CatchableError:
            discard
        continue
      else:
        res = e.msg; exitCode = 1
    except CatchableError as e:
      if not reconnectRedisClientMs(cfg.redisUrl, client, remainingMs, isForever):
        return
      if workerName.len > 0:
        try:
          discard client.setEx(cfg.prefix & "heartbeat:" & workerName, hbTtl, "1")
          discard client.sadd(cfg.prefix & "active_agents", workerName)
        except CatchableError:
          discard
      continue

    if exitCode == 0 and res.len > 0 and res != "(nil)" and res.strip().startsWith("{"):
      backoffMs = 250
      var parsed: JsonNode
      try:
        parsed = parseJson(res.strip())
      except JsonParsingError:
        stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping non-JSON claimed task")
        quit(1)

      let id = parsed.getOrDefault("id").getStr("")

      if runId.len > 0 and isRunCancelled(client, cfg, runId):
        stderr.writeLine("Run " & runId & " was cancelled. Discarding task and exiting.")
        discard doAck(cfg, queueName, id)
        return

      let fromAgent = parsed.getOrDefault("from").getStr("")
      let toAgent = parsed.getOrDefault("to").getStr("")
      let msgType = parsed.getOrDefault("type").getStr("")
      let subject = parsed.getOrDefault("subject").getStr("")
      let body = parsed.getOrDefault("body").getStr("")
      let ts = parsed.getOrDefault("timestamp").getStr("")
      let sig = parsed.getOrDefault("sig").getStr("")
      let isEncrypted = parsed.getOrDefault("encrypted").getBool(false)

      let canonical = id & "|" & fromAgent & "|" & toAgent & "|" & msgType & "|" & subject & "|" & body & "|" & ts
      if not verifyHmac(secret, canonical, sig):
        stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping unauthenticated/tampered task (ID: " & id & ")")
        quit(1)

      if isEncrypted:
        try:
          let decryptedBody = decryptAes(body, secret, cfg)
          parsed["body"] = %decryptedBody
          parsed["encrypted"] = %false
        except ValueError as e:
          stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping corrupted task: " & e.msg)
          quit(1)

      if rawOutput:
        echo parsed.getOrDefault("body").getStr("")
      else:
        echo $parsed
      return

    if effectiveTimeout == 0:
      return

    if workerName.len > 0:
      try:
        discard client.setEx(cfg.prefix & "heartbeat:" & workerName, hbTtl, "1")
        discard client.sadd(cfg.prefix & "active_agents", workerName)
      except CatchableError:
        discard

    let elapsed = epochTime() - startTime
    remainingMs = max(0, int((float(effectiveTimeout) - elapsed) * 1000))
    if remainingMs <= 0:
      return

    let sleepTime = min(backoffMs, remainingMs)
    sleep(sleepTime)
    backoffMs = min(2000, backoffMs * 2)

proc doAck*(cfg: LocutusConfig, queueName, taskId: string): int =
  let resStr = runLuaScript(cfg.redisUrl, ackLua, ackSha, [cfg.prefix, queueName, taskId])
  var res = 0
  try:
    res = parseInt(resStr.strip())
  except ValueError:
    res = 0

  if res == 1:
    echo "ACK: " & taskId
  else:
    stderr.writeLine("Warning: Task " & taskId & " not found or already acknowledged.")
  return res

proc doClaimRenew*(cfg: LocutusConfig, queueName, taskId: string, leaseSec: int = 120) =
  let res = runLuaScript(cfg.redisUrl, claimRenewLua, claimRenewSha, [cfg.prefix, queueName, taskId, $leaseSec])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  echo res


proc decryptBlackboardValue(raw: string, secret: string, cfg: LocutusConfig, contextMsg: string): string =
  if not raw.startsWith("aes256:"):
    return raw
  let parts = raw.split(":")
  if parts.len >= 3:
    let sig = parts[1]
    let cipher = parts[2..^1].join(":")
    if not verifyHmac(secret, cipher, sig):
      stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping unauthenticated/tampered blackboard entry: " & contextMsg)
      quit(1)
    try:
      return decryptAes(cipher, secret, cfg)
    except ValueError as e:
      stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping corrupted blackboard entry: " & e.msg)
      quit(1)
  elif parts.len == 2:
    try:
      return decryptAes(parts[1], secret, cfg)
    except ValueError as e:
      stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping corrupted blackboard entry: " & e.msg)
      quit(1)
  return raw

proc doBlackboard*(cfg: LocutusConfig, action, room: string, key: string = "", val: string = "", ttlSec: int = -1): string =
  let effectiveTtl = if ttlSec >= 0: ttlSec else: (if cfg.messageTtl > 0: cfg.messageTtl else: 604800)
  let secret = getSecret(cfg)
  var finalVal = val
  if cfg.encrypt and (action == "set" or action == "append") and val.len > 0:
    let cipher = encryptAes(val, secret, cfg)
    let sig = computeHmacSha256(secret, cipher)
    finalVal = "aes256:" & sig & ":" & cipher

  let res = runLuaScript(cfg.redisUrl, blackboardLua, blackboardSha, [cfg.prefix, action, room, key, finalVal, $effectiveTtl])
  if res == "(nil)":
    return ""

  let trimmed = res.strip()
  if action == "get":
    if trimmed.startsWith("aes256:"):
      return decryptBlackboardValue(trimmed, secret, cfg, "room: " & room & ", key: " & key)
    elif trimmed.startsWith("["):
      try:
        let j = parseJson(trimmed)
        if j.kind == JArray:
          var decryptedArr = newJArray()
          for item in j.elems:
            let s = item.getStr("")
            if s.startsWith("aes256:"):
              decryptedArr.add(%decryptBlackboardValue(s, secret, cfg, "room: " & room & ", key: " & key))
            else:
              decryptedArr.add(item)
          return $decryptedArr
      except JsonParsingError:
        discard
    return trimmed

  elif action == "snapshot":
    try:
      var j = parseJson(trimmed)
      if j.hasKey("kv") and j["kv"].kind == JObject:
        var newKv = newJObject()
        for k, v in j["kv"].pairs:
          let s = v.getStr("")
          if s.startsWith("aes256:"):
            newKv[k] = %decryptBlackboardValue(s, secret, cfg, "room: " & room & ", key: " & k)
          else:
            newKv[k] = v
        j["kv"] = newKv

      if j.hasKey("lists") and j["lists"].kind == JObject:
        var newLists = newJObject()
        for lk, lv in j["lists"].pairs:
          if lv.kind == JArray:
            var newArr = newJArray()
            for item in lv.elems:
              let s = item.getStr("")
              if s.startsWith("aes256:"):
                newArr.add(%decryptBlackboardValue(s, secret, cfg, "room: " & room & ", list: " & lk))
              else:
                newArr.add(item)
            newLists[lk] = newArr
          else:
            newLists[lk] = lv
        j["lists"] = newLists
      return $j
    except JsonParsingError:
      return trimmed

  return trimmed

proc doBlackboardLoad*(cfg: LocutusConfig, room, snapshotJson: string, ttlSec: int = 0): string =
  var parsed: JsonNode
  try:
    parsed = parseJson(snapshotJson)
  except JsonParsingError as e:
    stderr.writeLine("ERR: Invalid JSON snapshot for blackboard load: " & e.msg)
    quit(1)

  if parsed.kind != JObject:
    stderr.writeLine("ERR: Invalid JSON snapshot for blackboard load: root must be a JSON object")
    quit(1)

  let secret = getSecret(cfg)
  var toLoad = parsed

  if cfg.encrypt:
    if toLoad.hasKey("kv") and toLoad["kv"].kind == JObject:
      var encKv = newJObject()
      for k, v in toLoad["kv"].pairs:
        let rawStr = if v.kind == JString: v.getStr() else: $v
        let cipher = encryptAes(rawStr, secret, cfg)
        let sig = computeHmacSha256(secret, cipher)
        encKv[k] = %("aes256:" & sig & ":" & cipher)
      toLoad["kv"] = encKv

    if toLoad.hasKey("lists") and toLoad["lists"].kind == JObject:
      var encLists = newJObject()
      for lk, lv in toLoad["lists"].pairs:
        if lv.kind == JArray:
          var encArr = newJArray()
          for item in lv.elems:
            let rawStr = if item.kind == JString: item.getStr() else: $item
            let cipher = encryptAes(rawStr, secret, cfg)
            let sig = computeHmacSha256(secret, cipher)
            encArr.add(%("aes256:" & sig & ":" & cipher))
          encLists[lk] = encArr
        else:
          encLists[lk] = lv
      toLoad["lists"] = encLists

  let effectiveTtl = if ttlSec > 0: ttlSec else: (if cfg.messageTtl > 0: cfg.messageTtl else: 0)
  let res = runLuaScript(cfg.redisUrl, blackboardLua, blackboardSha, [cfg.prefix, "load", room, "", $toLoad, $effectiveTtl])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  return res

proc doFloorRequest*(cfg: LocutusConfig, room, agentName: string, waitSec: int = 0, leaseSec: int = 60) =
  let startTime = getTime().toUnix()
  var remaining = waitSec

  var res = runLuaScript(cfg.redisUrl, floorLua, floorSha, [cfg.prefix, "request", room, agentName, $leaseSec])
  if res == "ACQUIRED":
    echo "ACQUIRED: " & agentName & " holds floor in " & room
    return

  if waitSec <= 0:
    let holder = if res.startsWith("BUSY:"): res[5..^1] else: "unknown"
    stderr.writeLine("Floor in " & room & " is held by " & holder)
    quit(1)

  discard runLuaScript(cfg.redisUrl, floorLua, floorSha, [cfg.prefix, "enqueue_waiter", room, agentName])

  while remaining > 0:
    sleep(150)
    res = runLuaScript(cfg.redisUrl, floorLua, floorSha, [cfg.prefix, "request", room, agentName, $leaseSec])
    if res == "ACQUIRED":
      echo "ACQUIRED: " & agentName & " holds floor in " & room
      return
    let elapsed = int(getTime().toUnix() - startTime)
    remaining = max(0, waitSec - elapsed)

  # Dequeue from waiters list on timeout to prevent stale waiter hijacking subsequent yields
  try:
    var client = openRedisClient(cfg.redisUrl)
    defer: (try: client.close() except CatchableError: discard)
    discard client.lRem(cfg.prefix & "floor:{" & room & "}:waiters", agentName, 0)
  except CatchableError:
    discard

  let holder = if res.startsWith("BUSY:"): res[5..^1] else: "unknown"
  stderr.writeLine("Timeout waiting for floor in " & room & ". Currently held by " & holder)
  quit(1)

proc doFloorYield*(cfg: LocutusConfig, room, agentName: string, force: bool = false, leaseSec: int = 60) =
  let forceArg = if force: "force" else: ""
  let res = runLuaScript(cfg.redisUrl, floorLua, floorSha, [cfg.prefix, "yield", room, agentName, forceArg, $leaseSec])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  echo res

proc doFloorPass*(cfg: LocutusConfig, room, agentName, targetAgent: string, force: bool = false, leaseSec: int = 60) =
  let forceArg = if force: "force" else: ""
  let res = runLuaScript(cfg.redisUrl, floorLua, floorSha, [cfg.prefix, "pass", room, agentName, targetAgent, $leaseSec, forceArg])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  echo res

proc doFloorStatus*(cfg: LocutusConfig, room: string) =
  let res = runLuaScript(cfg.redisUrl, floorLua, floorSha, [cfg.prefix, "status", room])
  echo res

proc doCancelSet*(cfg: LocutusConfig, runId, reason, byAgent: string, ttlSec: int = 3600) =
  let secret = getSecret(cfg)
  let ts = now().utc.format("yyyy-MM-dd'T'HH:mm:ss'Z'")
  let canonical = runId & "|" & reason & "|" & byAgent & "|" & ts
  let sig = computeHmacSha256(secret, canonical)
  let res = runLuaScript(cfg.redisUrl, cancelLua, cancelSha, [cfg.prefix, "cancel", runId, reason, byAgent, $ttlSec, ts, sig])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  var n = newJObject()
  n["status"] = %"cancelled"
  n["run_id"] = %runId
  n["reason"] = %reason
  n["by"] = %byAgent
  n["timestamp"] = %ts
  n["sig"] = %sig
  n["cancelled"] = %true
  echo $n

proc doCancelCheck*(cfg: LocutusConfig, runId: string, rawOutput: bool = false, exitCodeOnUncancelled: bool = false) =
  let res = runLuaScript(cfg.redisUrl, cancelLua, cancelSha, [cfg.prefix, "check", runId])
  if res.len == 0 or res == "(nil)":
    if exitCodeOnUncancelled:
      quit(1)
    return

  try:
    let parsed = parseJson(res)
    let secret = getSecret(cfg)
    let reason = parsed.getOrDefault("reason").getStr("")
    let byAgent = parsed.getOrDefault("by").getStr("")
    let ts = parsed.getOrDefault("timestamp").getStr("")
    let sig = parsed.getOrDefault("sig").getStr("")
    let canonical = runId & "|" & reason & "|" & byAgent & "|" & ts

    if sig.len == 0 or not verifyHmac(secret, canonical, sig):
      stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping unauthenticated/tampered cancellation token (run_id: " & runId & ")")
      if exitCodeOnUncancelled:
        quit(1)
      return

    if rawOutput:
      echo reason
    else:
      echo res
  except JsonParsingError:
    if exitCodeOnUncancelled:
      quit(1)
    return

proc doCancelClear*(cfg: LocutusConfig, runId: string) =
  let res = runLuaScript(cfg.redisUrl, cancelLua, cancelSha, [cfg.prefix, "clear", runId])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  echo res

proc doBallotOpen*(cfg: LocutusConfig, ballotId, options, voters: string, ttlSec: int = 3600) =
  let ts = now().utc.format("yyyy-MM-dd'T'HH:mm:ss'Z'")
  let res = runLuaScript(cfg.redisUrl, ballotLua, ballotSha, [cfg.prefix, "open", ballotId, options, voters, $ttlSec, ts])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  echo res

proc doBallotCast*(cfg: LocutusConfig, ballotId, voter, choice: string) =
  let secret = getSecret(cfg)
  let ts = now().utc.format("yyyy-MM-dd'T'HH:mm:ss'Z'")
  let canonical = voter & "|" & ballotId & "|" & choice & "|" & ts
  let sig = computeHmacSha256(secret, canonical)
  let res = runLuaScript(cfg.redisUrl, ballotLua, ballotSha, [cfg.prefix, "cast", ballotId, voter, choice, sig, ts])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  echo res

proc doBallotTally*(cfg: LocutusConfig, ballotId: string, closeBallot: bool = false, rawOutput: bool = false) =
  let closeArg = if closeBallot: "close" else: ""
  let res = runLuaScript(cfg.redisUrl, ballotLua, ballotSha, [cfg.prefix, "tally", ballotId, closeArg])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)

  var parsed: JsonNode
  try:
    parsed = parseJson(res)
  except JsonParsingError:
    echo res
    return

  let secret = getSecret(cfg)
  var verifiedTally = newJObject()
  var verifiedTotal = 0

  if parsed.hasKey("tally") and parsed["tally"].kind == JObject:
    for k, _ in parsed["tally"].pairs:
      verifiedTally[k] = %0

  if parsed.hasKey("votes") and parsed["votes"].kind == JObject:
    for voter, vInfo in parsed["votes"].pairs:
      let choice = vInfo.getOrDefault("choice").getStr("")
      let sigEntry = vInfo.getOrDefault("sig_entry").getStr("")
      var valid = false
      if sigEntry.len > 0 and "|" in sigEntry:
        let p = sigEntry.split("|")
        if p.len >= 2:
          let sig = p[0]
          let ts = p[1..^1].join("|")
          let canonical = voter & "|" & ballotId & "|" & choice & "|" & ts
          if verifyHmac(secret, canonical, sig):
            valid = true

      if valid:
        verifiedTotal.inc
        let curr = verifiedTally.getOrDefault(choice).getInt(0)
        verifiedTally[choice] = %(curr + 1)
      else:
        stderr.writeLine("[LOCUTUS SECURITY] WARNING: Discarding unauthenticated/tampered vote from voter: " & voter)

    parsed["total_votes"] = %verifiedTotal
    parsed["tally"] = verifiedTally

    var maxCount = -1
    var winner = ""
    for k, v in verifiedTally.pairs:
      let cnt = v.getInt(0)
      if cnt > maxCount:
        maxCount = cnt
        winner = k
    parsed["winner"] = %winner
    parsed.delete("votes")

  if rawOutput:
    echo parsed.getOrDefault("winner").getStr("")
  else:
    echo $parsed

proc doBallotStatus*(cfg: LocutusConfig, ballotId: string) =
  let res = runLuaScript(cfg.redisUrl, ballotLua, ballotSha, [cfg.prefix, "status", ballotId])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  echo res

proc doLeaderAcquire*(cfg: LocutusConfig, role, agentName: string, leaseSec: int = 30) =
  let secret = getSecret(cfg)
  let ts = now().utc.format("yyyy-MM-dd'T'HH:mm:ss'Z'")
  let canonical = role & "|" & agentName & "|" & ts & "|" & $leaseSec
  let sig = computeHmacSha256(secret, canonical)
  var res = runLuaScript(cfg.redisUrl, leaderLua, leaderSha, [cfg.prefix, "acquire", role, agentName, $leaseSec, ts, sig])
  if res.startsWith("HELD:"):
    var existing = ""
    try:
      var client = openRedisClient(cfg.redisUrl)
      defer: (try: client.close() except CatchableError: discard)
      let val = client.get(cfg.prefix & "leader:{" & role & "}")
      if val != redisNil:
        existing = val
    except CatchableError:
      discard
    if existing.len > 0:
      try:
        let parsed = parseJson(existing)
        let exLeader = parsed.getOrDefault("leader").getStr("")
        let exTs = parsed.getOrDefault("acquired_at").getStr("")
        let exLease = parsed.getOrDefault("lease_sec").getInt(0)
        let exSig = parsed.getOrDefault("sig").getStr("")
        let exCanonical = role & "|" & exLeader & "|" & exTs & "|" & $exLease
        if exSig.len == 0 or not verifyHmac(secret, exCanonical, exSig):
          stderr.writeLine("[LOCUTUS SECURITY] WARNING: Preempting unauthenticated/forged leader key for role: " & role)
          res = runLuaScript(cfg.redisUrl, leaderLua, leaderSha, [cfg.prefix, "acquire", role, agentName, $leaseSec, ts, sig, "force"])
      except JsonParsingError:
        stderr.writeLine("[LOCUTUS SECURITY] WARNING: Preempting corrupt leader key for role: " & role)
        res = runLuaScript(cfg.redisUrl, leaderLua, leaderSha, [cfg.prefix, "acquire", role, agentName, $leaseSec, ts, sig, "force"])

  if res.startsWith("HELD:") or res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  echo res

proc doLeaderRenew*(cfg: LocutusConfig, role, agentName: string, leaseSec: int = 30) =
  let secret = getSecret(cfg)
  let ts = now().utc.format("yyyy-MM-dd'T'HH:mm:ss'Z'")
  let canonical = role & "|" & agentName & "|" & ts & "|" & $leaseSec
  let sig = computeHmacSha256(secret, canonical)
  let res = runLuaScript(cfg.redisUrl, leaderLua, leaderSha, [cfg.prefix, "renew", role, agentName, $leaseSec, ts, sig])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  echo res

proc doLeaderResign*(cfg: LocutusConfig, role, agentName: string) =
  let res = runLuaScript(cfg.redisUrl, leaderLua, leaderSha, [cfg.prefix, "resign", role, agentName])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  echo res

proc doLeaderStatus*(cfg: LocutusConfig, role: string) =
  let res = runLuaScript(cfg.redisUrl, leaderLua, leaderSha, [cfg.prefix, "status", role])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)

  try:
    let parsed = parseJson(res)
    let secret = getSecret(cfg)
    let leader = parsed.getOrDefault("leader").getStr("")
    let status = parsed.getOrDefault("status").getStr("")
    if status == "active" and leader.len > 0:
      let exTs = parsed.getOrDefault("acquired_at").getStr("")
      let exLease = parsed.getOrDefault("lease_sec").getInt(0)
      let exSig = parsed.getOrDefault("sig").getStr("")
      let canonical = role & "|" & leader & "|" & exTs & "|" & $exLease
      if exSig.len == 0 or not verifyHmac(secret, canonical, exSig):
        stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping unauthenticated/tampered leader key for role: " & role)
        var vacant = newJObject()
        vacant["role"] = %role
        vacant["leader"] = %""
        vacant["status"] = %"vacant"
        vacant["ttl"] = %0
        echo $vacant
        return
    echo res
  except JsonParsingError:
    echo res

proc doWorkflowDefine*(cfg: LocutusConfig, flowId, steps, deps: string, ttlSec: int = 86400) =
  let ts = now().utc.format("yyyy-MM-dd'T'HH:mm:ss'Z'")
  let res = runLuaScript(cfg.redisUrl, workflowLua, workflowSha, [cfg.prefix, "define", flowId, steps, deps, $ttlSec, ts])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  echo res

proc doWorkflowNext*(cfg: LocutusConfig, flowId: string, rawOutput: bool = false) =
  let res = runLuaScript(cfg.redisUrl, workflowLua, workflowSha, [cfg.prefix, "next", flowId])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  if rawOutput:
    try:
      let n = parseJson(res)
      if n.hasKey("ready"):
        for item in n["ready"]:
          echo item.getStr()
    except CatchableError:
      echo res
  else:
    echo res

proc doWorkflowResolve*(cfg: LocutusConfig, flowId, step, output: string, rawOutput: bool = false) =
  let ts = now().utc.format("yyyy-MM-dd'T'HH:mm:ss'Z'")
  let secret = getSecret(cfg)
  var finalOutput = output
  if cfg.encrypt and output.len > 0:
    finalOutput = "aes256:" & encryptAes(output, secret, cfg)
  let res = runLuaScript(cfg.redisUrl, workflowLua, workflowSha, [cfg.prefix, "resolve", flowId, step, finalOutput, ts])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  if rawOutput:
    try:
      let n = parseJson(res)
      if n.hasKey("unlocked"):
        for item in n["unlocked"]:
          echo item.getStr()
    except CatchableError:
      echo res
  else:
    echo res

proc doWorkflowFail*(cfg: LocutusConfig, flowId, step, reason: string) =
  let res = runLuaScript(cfg.redisUrl, workflowLua, workflowSha, [cfg.prefix, "fail", flowId, step, reason])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  echo res

proc doWorkflowStatus*(cfg: LocutusConfig, flowId: string, rawOutput: bool = false) =
  let res = runLuaScript(cfg.redisUrl, workflowLua, workflowSha, [cfg.prefix, "status", flowId])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)

  var parsed: JsonNode
  try:
    parsed = parseJson(res)
    let secret = getSecret(cfg)
    if parsed.hasKey("steps") and parsed["steps"].kind == JObject:
      for stepName, stepObj in parsed["steps"].pairs:
        if stepObj.hasKey("output"):
          let outStr = stepObj["output"].getStr("")
          if outStr.startsWith("aes256:"):
            try:
              stepObj["output"] = %decryptAes(outStr[7..^1], secret, cfg)
            except ValueError:
              discard
  except JsonParsingError:
    if rawOutput:
      echo res
    else:
      echo res
    return

  if rawOutput:
    try:
      if parsed.hasKey("status"):
        echo parsed["status"].getStr()
      else:
        echo $parsed
    except CatchableError:
      echo res
  else:
    echo $parsed

proc doWorkflowExport*(cfg: LocutusConfig, flowId: string, outputFile: string = "") =
  let res = runLuaScript(cfg.redisUrl, workflowLua, workflowSha, [cfg.prefix, "status", flowId])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)

  var parsed: JsonNode
  try:
    parsed = parseJson(res)
    let secret = getSecret(cfg)
    if parsed.hasKey("steps") and parsed["steps"].kind == JObject:
      for stepName, stepObj in parsed["steps"].pairs:
        if stepObj.hasKey("output"):
          let outStr = stepObj["output"].getStr("")
          if outStr.startsWith("aes256:"):
            try:
              stepObj["output"] = %decryptAes(outStr[7..^1], secret, cfg)
            except ValueError:
              discard
  except JsonParsingError:
    stderr.writeLine("ERR: Failed to parse workflow state: " & res)
    quit(1)

  let formatted = $parsed
  if outputFile.len > 0:
    try:
      writeFile(outputFile, formatted)
      echo "OK"
    except CatchableError as e:
      stderr.writeLine("Error writing workflow export to file '" & outputFile & "': " & e.msg)
      quit(1)
  else:
    echo formatted

proc doWorkflowImport*(cfg: LocutusConfig, flowId, fileOrJson: string, ttlSec: int = 0) =
  var rawJson = fileOrJson
  if fileExists(fileOrJson):
    try:
      rawJson = readFile(fileOrJson)
    except CatchableError as e:
      stderr.writeLine("Error reading workflow file '" & fileOrJson & "': " & e.msg)
      quit(1)
  elif fileOrJson.startsWith("@") and fileExists(fileOrJson[1..^1]):
    try:
      rawJson = readFile(fileOrJson[1..^1])
    except CatchableError as e:
      stderr.writeLine("Error reading workflow file '" & fileOrJson[1..^1] & "': " & e.msg)
      quit(1)

  var parsed: JsonNode
  try:
    parsed = parseJson(rawJson)
  except JsonParsingError as e:
    stderr.writeLine("ERR: Invalid JSON workflow payload: " & e.msg)
    quit(1)

  if parsed.kind != JObject or not parsed.hasKey("steps") or not parsed.hasKey("status"):
    stderr.writeLine("ERR: Invalid JSON workflow payload: must contain 'steps' and 'status'")
    quit(1)

  if cfg.encrypt:
    let secret = getSecret(cfg)
    if parsed.hasKey("steps") and parsed["steps"].kind == JObject:
      for stepName, stepObj in parsed["steps"].pairs:
        if stepObj.hasKey("output"):
          let outStr = stepObj["output"].getStr("")
          if outStr.len > 0 and not outStr.startsWith("aes256:"):
            stepObj["output"] = %("aes256:" & encryptAes(outStr, secret, cfg))

  let res = runLuaScript(cfg.redisUrl, workflowLua, workflowSha, [cfg.prefix, "import", flowId, $parsed, $ttlSec])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)
  echo res

proc doSweep*(cfg: LocutusConfig, dryRun: bool = false, rawOutput: bool = false) =
  let action = if dryRun: "audit" else: "prune"
  let dryRunArg = if dryRun: "1" else: "0"
  let res = runLuaScript(cfg.redisUrl, sweepLua, sweepSha, [cfg.prefix, action, dryRunArg])
  if res.startsWith("ERR:"):
    stderr.writeLine(res)
    quit(1)

  var prunedAgents: seq[string] = @[]
  var prunedListeners: seq[string] = @[]
  var foreignListeners: seq[JsonNode] = @[]
  let currentHost = getHostNameStr()

  try:
    let n = parseJson(res)
    if n.hasKey("dead_agents"):
      for a in n["dead_agents"]:
        prunedAgents.add(a.getStr())
    if n.hasKey("listeners"):
      var keysToDelete: seq[string] = @[]
      for l in n["listeners"]:
        let agent = l["agent"].getStr()
        let lKey = l["key"].getStr()
        let dataStr = l["data"].getStr()
        if dataStr.len > 0:
          try:
            let lockData = parseJson(dataStr)
            let host = lockData.getOrDefault("host").getStr()
            let pid = lockData.getOrDefault("pid").getInt()
            if host == currentHost:
              if pid > 0 and not isPidAlive(pid):
                prunedListeners.add(agent)
                if not dryRun:
                  keysToDelete.add(lKey)
            else:
              foreignListeners.add(%*{
                "agent": agent,
                "host": host,
                "pid": pid,
                "status": "foreign_active"
              })
          except CatchableError:
            discard
      if not dryRun and keysToDelete.len > 0:
        try:
          var client = openRedisClient(cfg.redisUrl)
          defer: (try: client.close() except CatchableError: discard)
          discard client.del(keysToDelete)
        except CatchableError:
          discard
  except CatchableError as e:
    stderr.writeLine("Error parsing sweep results: " & e.msg)
    quit(1)

  if rawOutput:
    var msg = "Pruned " & $prunedAgents.len & " dead agents, " & $prunedListeners.len & " stale listeners."
    if foreignListeners.len > 0:
      msg.add(" Skipped " & $foreignListeners.len & " foreign host listeners.")
    echo msg
  else:
    var outObj = %*{
      "pruned_agents": %prunedAgents,
      "pruned_listeners": %prunedListeners,
      "foreign_listeners": %foreignListeners,
      "dry_run": %dryRun
    }
    echo $outObj

proc doRequest*(cfg: LocutusConfig, toAgent, fromAgent, subject, body: string, timeoutSec: int = 30, rawOutput: bool = false) =
  randomize()
  let secret = getSecret(cfg)
  let reqId = "req_" & $getTime().toUnix() & "_" & fromAgent & "_" & $rand(1000..9999)
  let replyQueue = "reply:" & reqId
  let replyInboxKey = cfg.prefix & "inbox:" & replyQueue

  discard doSend(cfg, toAgent, "task", fromAgent, subject, body, tags = @[], replyTo = replyQueue, msgId = reqId, isBroadcast = false, echoResult = false)

  var client = connectRedis(cfg.redisUrl)
  defer:
    try: client.close() except CatchableError: discard

  let startTime = getTime().toUnix()
  var remaining = timeoutSec

  while remaining > 0:
    var popRes: RedisList
    try:
      popRes = client.bRPop(@[replyInboxKey], remaining)
    except CatchableError as e:
      stderr.writeLine("Redis error: " & e.msg)
      quit(1)

    if popRes.len == 0 or popRes.len < 2:
      stderr.writeLine("Error: Request timed out waiting for reply from " & toAgent)
      quit(1)

    let payloadStr = popRes[1].strip()

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

proc doScatter*(cfg: LocutusConfig, targets, fromAgent, subject, body: string,
               quorum: int = -1, timeoutSec: int = 30, rawOutput: bool = false) =
  randomize()
  let secret = getSecret(cfg)
  let scatterId = "sc_" & $getTime().toUnix() & "_" & fromAgent & "_" & $rand(1000..9999)
  let replyQueue = "scatter:" & scatterId
  let replyInboxKey = cfg.prefix & "inbox:" & replyQueue

  let ts = now().utc.format("yyyy-MM-dd'T'HH:mm:ss'Z'")
  let finalBody = if cfg.encrypt: encryptAes(body, secret, cfg) else: body
  let canonical = scatterId & "|" & fromAgent & "|" & targets & "|task|" & subject & "|" & finalBody & "|" & ts
  let sig = computeHmacSha256(secret, canonical)

  var node = newJObject()
  node["id"] = %scatterId
  node["from"] = %fromAgent
  node["to"] = %targets
  node["type"] = %"task"
  node["reply_to"] = %replyQueue
  node["tags"] = newJArray()
  node["subject"] = %subject
  node["body"] = %finalBody
  node["timestamp"] = %ts
  node["sig"] = %sig
  node["encrypted"] = %cfg.encrypt

  let msgJson = $node
  let effectiveTtl = if cfg.messageTtl > 0: cfg.messageTtl else: 300

  let deliveredStr = runLuaScript(cfg.redisUrl, scatterLua, scatterSha, [cfg.prefix, targets, msgJson, $effectiveTtl])
  var delivered = 0
  try:
    delivered = parseInt(deliveredStr.strip())
  except ValueError:
    delivered = 0

  let effectiveQuorum = if delivered <= 0:
                          0
                        elif quorum >= 0:
                          min(quorum, delivered)
                        else:
                          delivered

  var collectedReplies: seq[JsonNode] = @[]
  var seenSenders = initHashSet[string]()

  if effectiveQuorum > 0 and delivered > 0:
    var client = connectRedis(cfg.redisUrl)
    defer:
      try: client.close() except CatchableError: discard

    try:
      discard client.expire(replyInboxKey, timeoutSec + 60)
    except CatchableError:
      discard
    let startTime = getTime().toUnix()
    var remaining = timeoutSec

    while collectedReplies.len < effectiveQuorum and remaining > 0:
      var popRes: RedisList
      try:
        popRes = client.bRPop(@[replyInboxKey], remaining)
      except CatchableError as e:
        stderr.writeLine("Redis error: " & e.msg)
        break

      if popRes.len == 0 or popRes.len < 2:
        break

      let payloadStr = popRes[1].strip()
      var parsed: JsonNode
      try:
        parsed = parseJson(payloadStr)
      except JsonParsingError:
        stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping non-JSON payload from scatter inbox")
        let elapsed = int(getTime().toUnix() - startTime)
        remaining = max(0, timeoutSec - elapsed)
        continue

      let id = parsed.getOrDefault("id").getStr("")
      let sender = parsed.getOrDefault("from").getStr("")
      let toTarget = parsed.getOrDefault("to").getStr("")
      let msgType = parsed.getOrDefault("type").getStr("")
      let subj = parsed.getOrDefault("subject").getStr("")
      let bdy = parsed.getOrDefault("body").getStr("")
      let rts = parsed.getOrDefault("timestamp").getStr("")
      let rsig = parsed.getOrDefault("sig").getStr("")
      let isEncrypted = parsed.getOrDefault("encrypted").getBool(false)

      # Validate HMAC
      let rCanonical = id & "|" & sender & "|" & toTarget & "|" & msgType & "|" & subj & "|" & bdy & "|" & rts
      if not verifyHmac(secret, rCanonical, rsig):
        stderr.writeLine("[LOCUTUS SECURITY] WARNING: Dropping unauthenticated/tampered reply (ID: " & id & ")")
        let elapsed = int(getTime().toUnix() - startTime)
        remaining = max(0, timeoutSec - elapsed)
        continue

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

      if not seenSenders.contains(sender):
        seenSenders.incl(sender)
        collectedReplies.add(parsed)

      let elapsed = int(getTime().toUnix() - startTime)
      remaining = max(0, timeoutSec - elapsed)

    try:
      discard client.del(@[replyInboxKey])
    except CatchableError:
      discard

  if rawOutput:
    for r in collectedReplies:
      echo r["body"].getStr("")
  else:
    var resArr = newJArray()
    for r in collectedReplies:
      resArr.add(r)
    echo $resArr

proc doPub*(cfg: LocutusConfig, channel, message: string): string =
  let fullChan = cfg.prefix & "channel:" & channel
  var client = connectRedis(cfg.redisUrl)
  defer:
    try: client.close() except CatchableError: discard
  try:
    let receivers = client.publish(fullChan, message)
    return $receivers
  except CatchableError as e:
    stderr.writeLine("Redis error: " & e.msg)
    quit(1)

proc doSub*(cfg: LocutusConfig, channel: string, timeoutSec: int = -1) =
  let fullChan = cfg.prefix & "channel:" & channel
  proc asyncSub(): Future[string] {.async.} =
    let parsed = parseRedisUrl(cfg.redisUrl)
    let r = await openAsync(parsed.host, parsed.port.Port)
    if parsed.password.len > 0:
      await r.auth(parsed.password)
    if parsed.db != 0:
      discard await r.select(parsed.db)
    await r.subscribe(fullChan)
    if timeoutSec > 0:
      let msgFut = r.nextMessage()
      if await withTimeout(msgFut, timeoutSec * 1000):
        let msg = msgFut.read()
        try:
          await r.close()
        except CatchableError:
          discard
        return msg.message
      else:
        try:
          await r.close()
        except CatchableError:
          discard
        return ""
    else:
      let msg = await r.nextMessage()
      try:
        await r.close()
      except CatchableError:
        discard
      return msg.message

  try:
    let res = waitFor asyncSub()
    if res.len > 0:
      echo res
  except CatchableError as e:
    stderr.writeLine("Redis error: " & e.msg)
    quit(1)

proc resolveVal(val: string): string =
  if val.startsWith("@") and val.len > 1 and fileExists(val[1..^1]):
    try:
      return readFile(val[1..^1])
    except CatchableError:
      return val
  return val

# Main Entrypoint / CLI Router
proc main() =
  installSignalHandlers()
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
    elif a.startsWith("--valkey-url="):
      cli.redisUrl = a[13..^1]
    elif (a == "--redis-url" or a == "--valkey-url" or a == "-u") and i + 1 < rawArgs.len:
      cli.redisUrl = rawArgs[i+1]; inc i
    elif a.startsWith("-u="):
      cli.redisUrl = a[3..^1]
    elif a.startsWith("--prefix="):
      cli.prefix = a[9..^1]
    elif a == "--prefix" and i + 1 < rawArgs.len and not rawArgs[i+1].startsWith("-"):
      cli.prefix = rawArgs[i+1]; inc i
    elif a.startsWith("--project="):
      cli.project = a[10..^1]
    elif a == "--project":
      let isConfigInit = ("config" in rawArgs) and ("init" in rawArgs)
      if not isConfigInit and i + 1 < rawArgs.len and not rawArgs[i+1].startsWith("-"):
        cli.project = rawArgs[i+1]; inc i
      else:
        positionalArgs.add(a)
    elif a.startsWith("--agent-name="):
      cli.agentName = a[13..^1]
    elif a == "--agent-name" and i + 1 < rawArgs.len and not rawArgs[i+1].startsWith("-"):
      cli.agentName = rawArgs[i+1]; inc i
    elif a.startsWith("--secret="):
      cli.secret = a[9..^1]
    elif a == "--secret" and i + 1 < rawArgs.len and not rawArgs[i+1].startsWith("-"):
      cli.secret = rawArgs[i+1]; inc i
    elif a.startsWith("--secret-file="):
      cli.secretFile = a[14..^1]
    elif a == "--secret-file" and i + 1 < rawArgs.len and not rawArgs[i+1].startsWith("-"):
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
      cli.timeout = some(parseRequiredInt(a[10..^1], "--timeout"))
    elif a == "--timeout" and i + 1 < rawArgs.len:
      cli.timeout = some(parseRequiredInt(rawArgs[i+1], "--timeout"))
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
    echo "  locutus open [name] [tags] [--listen/-l]"
    echo "  locutus listen [name] [timeout_sec] [--force/-f]"
    echo "  locutus send --to <agent> [--type task|query|reply|status] --subject <subj> --body <body> [--listen/-l]"
    echo "  locutus reply --to <agent> --subject <subj> --body <body> [--reply-to <id>] [--listen/-l]"
    echo "  locutus broadcast [--tags <tags>] --subject <subj> --body <body>"
    echo "  locutus request --to <agent> --subject <subj> --body <body> [--timeout 30] [--raw]"
    echo "  locutus scatter --targets <@tag|agents|*> --subject <subj> --body <body> [--quorum N] [--timeout 30] [--raw]"
    echo "  locutus enqueue <queue_name> --subject <subj> --body <body>"
    echo "  locutus work <queue_name> [timeout_sec]"
    echo "  locutus claim <queue_name> [timeout_sec] [--lease 120] [--raw]"
    echo "  locutus ack <queue_name> <task_id>"
    echo "  locutus blackboard <set|get|append|snapshot|load|delete|clear> <room> [key] [value]"
    echo "  locutus floor <request|yield|pass|status> <room> [args...]"
    echo "  locutus cancel <run_id> [--reason <reason>] | check <run_id> | clear <run_id>"
    echo "  locutus ballot <open|cast|tally|status> <ballot_id> [args...]"
    echo "  locutus leader <acquire|renew|resign|status> <role> [args...]"
    echo "  locutus workflow <define|next|resolve|fail|status|export|import> <flow_id> [args...]"
    echo "  locutus status <idle|busy|error> [activity_text] [name] [--listen/-l]"
    echo "  locutus lock <lock_name> [ttl_sec] [--fencing] [--raw]"
    echo "  locutus unlock <lock_name>"
    echo "  locutus pub <channel> <message>"
    echo "  locutus sub <channel> [timeout_sec]"
    echo "  locutus who [filter_tag]"
    echo "  locutus sweep [--dry-run] [--raw]"
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
    echo "  --redis-url, --valkey-url, -u <url> Redis / Valkey connection endpoint"
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
      var target = "workspace"
      var force = false
      var i = 2
      while i < args.len:
        let a = args[i]
        if a in ["-f", "--force"]:
          force = true
        elif a.toLowerAscii in ["--user", "-u", "user"]:
          target = "user"
        elif a.toLowerAscii in ["--project", "-p", "project", "--workspace", "-w", "workspace"]:
          target = "workspace"
        elif not a.startsWith("-"):
          target = a
        inc i
      let res = initConfigFile(target, force)
      if res.startsWith("Error:"):
        stderr.writeLine(res)
        quit(1)
      echo res
    else:
      stderr.writeLine("Unknown config action: " & action)
      stderr.writeLine("Usage: locutus config <show|get|path|init>")
      quit(1)

  of "open", "register":
    var name = ""
    var tags = ""
    var rearmListen = false
    var listenTimeout = -1
    var i = 1
    while i < args.len:
      let a = args[i]
      if a in ["--listen", "-l"]:
        rearmListen = true
      elif a.startsWith("--listen="):
        rearmListen = true
        try: listenTimeout = parseInt(a[9..^1])
        except ValueError:
          stderr.writeLine("Error: Invalid integer for --listen: '" & a[9..^1] & "'")
          quit(1)
      elif a.startsWith("--listen-timeout="):
        rearmListen = true
        try: listenTimeout = parseInt(a[17..^1])
        except ValueError:
          stderr.writeLine("Error: Invalid integer for --listen-timeout: '" & a[17..^1] & "'")
          quit(1)
      elif a == "--listen-timeout" and i + 1 < args.len:
        rearmListen = true
        try: listenTimeout = parseInt(args[i+1])
        except ValueError:
          stderr.writeLine("Error: Invalid integer for --listen-timeout: '" & args[i+1] & "'")
          quit(1)
        inc i
      elif not a.startsWith("-"):
        if name.len == 0: name = a
        elif tags.len == 0: tags = a
      inc i
    doOpen(cfg, name, tags, rearmListen, listenTimeout)

  of "listen":
    var explicitName = ""
    var timeout = cfg.listenTimeout
    var forceListen = false
    var i = 1
    while i < args.len:
      let a = args[i]
      if a in ["--force", "-f"]:
        forceListen = true
      elif not a.startsWith("-"):
        try:
          timeout = parseInt(a)
        except ValueError:
          if explicitName == "": explicitName = a
      inc i

    let name = getActiveAgentName(cfg, explicitName, fallbackDefault = false)
    if name.len == 0:
      stderr.writeLine("Error: No agent name specified. Run 'locutus open <name>', pass the agent name ('locutus listen <name>'), or export LOCUTUS_AGENT_NAME=<name>.")
      quit(1)

    if not forceListen:
      let (alreadyListening, existingPid, existingHost) = getActiveListenerInfo(cfg, name)
      if alreadyListening:
        stderr.writeLine("Error: Listener already active for agent '" & name & "' (PID " & $existingPid & " on " & existingHost & "). Refusing to start duplicate listener.")
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
      elif a.startsWith("--body="): body = resolveVal(a[7..^1])
      elif a == "--body" and i + 1 < args.len: body = resolveVal(args[i+1]); inc i
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
        listenTimeout = parseRequiredInt(a[9..^1], "--listen")
      elif a.startsWith("--listen-timeout="):
        rearmListen = true
        listenTimeout = parseRequiredInt(a[17..^1], "--listen-timeout")
      elif a == "--listen-timeout" and i + 1 < args.len:
        rearmListen = true
        listenTimeout = parseRequiredInt(args[i+1], "--listen-timeout")
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
    var timeout = if cfg.listenTimeout > 0: cfg.listenTimeout else: 30
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
      elif a.startsWith("--body="): body = resolveVal(a[7..^1])
      elif a == "--body" and i + 1 < args.len: body = resolveVal(args[i+1]); inc i
      elif a.startsWith("--timeout="):
        timeout = parseRequiredInt(a[10..^1], "--timeout")
      elif a == "--timeout" and i + 1 < args.len:
        timeout = parseRequiredInt(args[i+1], "--timeout")
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

  of "scatter":
    var targets = ""
    var subject = ""
    var body = ""
    var quorum = -1
    var timeout = if cfg.listenTimeout > 0: cfg.listenTimeout else: 30
    var rawOutput = false
    var fromAgent = getActiveAgentName(cfg, "")

    var i = 1
    while i < args.len:
      let a = args[i]
      if a.startsWith("--targets="): targets = a[10..^1]
      elif a == "--targets" and i + 1 < args.len: targets = args[i+1]; inc i
      elif a.startsWith("--target="): targets = a[9..^1]
      elif a == "--target" and i + 1 < args.len: targets = args[i+1]; inc i
      elif a.startsWith("--subject="): subject = a[10..^1]
      elif a == "--subject" and i + 1 < args.len: subject = args[i+1]; inc i
      elif a.startsWith("--body="): body = resolveVal(a[7..^1])
      elif a == "--body" and i + 1 < args.len: body = resolveVal(args[i+1]); inc i
      elif a.startsWith("--quorum="):
        quorum = parseRequiredInt(a[9..^1], "--quorum")
      elif a == "--quorum" and i + 1 < args.len:
        quorum = parseRequiredInt(args[i+1], "--quorum")
        inc i
      elif a.startsWith("--timeout="):
        timeout = parseRequiredInt(a[10..^1], "--timeout")
      elif a == "--timeout" and i + 1 < args.len:
        timeout = parseRequiredInt(args[i+1], "--timeout")
        inc i
      elif a == "--raw": rawOutput = true
      elif a.startsWith("--from="): fromAgent = a[7..^1]
      elif a == "--from" and i + 1 < args.len: fromAgent = args[i+1]; inc i
      elif not a.startsWith("-"):
        if targets == "": targets = a
        elif subject == "": subject = a
        elif body == "": body = a
      inc i

    if targets.len == 0 or subject.len == 0 or body.len == 0:
      stderr.writeLine("Error: Missing required arguments for scatter.")
      stderr.writeLine("Usage: locutus scatter --targets <@tag|agent1,agent2|*> --subject <subj> --body <body> [--quorum N] [--timeout sec] [--raw]")
      quit(1)

    doScatter(cfg, targets, fromAgent, subject, body, quorum, timeout, rawOutput)

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
      elif a.startsWith("--body="): body = resolveVal(a[7..^1])
      elif a == "--body" and i + 1 < args.len: body = resolveVal(args[i+1]); inc i
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
      stderr.writeLine("Usage: locutus work <queue_name> [timeout_sec] [--run-id <run_id>]")
      quit(1)
    let queueName = args[1]
    var timeout = -1
    var runId = ""
    var i = 2
    while i < args.len:
      let a = args[i]
      if a.startsWith("--run-id="):
        runId = a[9..^1]
      elif a == "--run-id" and i + 1 < args.len:
        runId = args[i+1]
        inc i
      elif not a.startsWith("-"):
        timeout = parseRequiredInt(a, "timeout")
      inc i
    doWork(cfg, queueName, timeout, runId)

  of "claim":
    if args.len < 2:
      stderr.writeLine("Error: Missing queue name.")
      stderr.writeLine("Usage: locutus claim <queue_name> [timeout_sec] [--lease 120] [--raw] [--run-id <run_id>]")
      stderr.writeLine("       locutus claim renew <queue_name> <task_id> [--lease 120]")
      quit(1)

    if args[1] == "renew":
      if args.len < 4:
        stderr.writeLine("Error: Missing queue name or task ID for claim renew.")
        stderr.writeLine("Usage: locutus claim renew <queue_name> <task_id> [--lease 120]")
        quit(1)
      let queueName = args[2]
      let taskId = args[3]
      var lease = 120
      var i = 4
      while i < args.len:
        let a = args[i]
        if a.startsWith("--lease="):
          lease = parseRequiredInt(a[8..^1], "--lease")
        elif a == "--lease" and i + 1 < args.len:
          lease = parseRequiredInt(args[i+1], "--lease")
          inc i
        inc i
      doClaimRenew(cfg, queueName, taskId, lease)
    else:
      let queueName = args[1]
      var timeout = -1
      var lease = 120
      var rawOutput = false
      var runId = ""

      var i = 2
      while i < args.len:
        let a = args[i]
        if a.startsWith("--lease="):
          lease = parseRequiredInt(a[8..^1], "--lease")
        elif a == "--lease" and i + 1 < args.len:
          lease = parseRequiredInt(args[i+1], "--lease")
          inc i
        elif a.startsWith("--timeout="):
          timeout = parseRequiredInt(a[10..^1], "--timeout")
        elif a == "--timeout" and i + 1 < args.len:
          timeout = parseRequiredInt(args[i+1], "--timeout")
          inc i
        elif a.startsWith("--run-id="):
          runId = a[9..^1]
        elif a == "--run-id" and i + 1 < args.len:
          runId = args[i+1]
          inc i
        elif a == "--raw":
          rawOutput = true
        elif not a.startsWith("-"):
          timeout = parseRequiredInt(a, "timeout")
        inc i

      doClaim(cfg, queueName, timeout, lease, rawOutput, runId)

  of "ack":
    if args.len < 3:
      stderr.writeLine("Error: Missing queue name or task ID.")
      stderr.writeLine("Usage: locutus ack <queue_name> <task_id>")
      quit(1)
    let queueName = args[1]
    let taskId = args[2]
    discard doAck(cfg, queueName, taskId)

  of "blackboard":
    if args.len < 3:
      stderr.writeLine("Usage: locutus blackboard <set|get|rev|append|snapshot|load|delete|clear> <room> [args...]")
      quit(1)
    let action = args[1].toLowerAscii
    let room = args[2]
    var key = ""
    var val = ""
    var ttlSec = -1

    var posArgs: seq[string] = @[]
    var i = 3
    while i < args.len:
      let a = args[i]
      if a.startsWith("--ttl="):
        ttlSec = parseRequiredInt(a[6..^1], "--ttl")
      elif a == "--ttl" and i + 1 < args.len:
        ttlSec = parseRequiredInt(args[i+1], "--ttl"); inc i
      elif not a.startsWith("-"):
        posArgs.add(a)
      inc i

    if posArgs.len > 0: key = posArgs[0]
    if posArgs.len > 1: val = posArgs[1]

    case action
    of "set":
      if key.len == 0 or val.len == 0:
        stderr.writeLine("Usage: locutus blackboard set <room> <key> <json_value> [--ttl <sec>]")
        quit(1)
      let res = doBlackboard(cfg, "set", room, key, resolveVal(val), ttlSec)
      echo res
    of "get":
      if key.len == 0:
        stderr.writeLine("Usage: locutus blackboard get <room> <key>")
        quit(1)
      let res = doBlackboard(cfg, "get", room, key)
      if res.len > 0:
        echo res
    of "rev":
      if key.len == 0:
        stderr.writeLine("Usage: locutus blackboard rev <room> <key>")
        quit(1)
      let res = doBlackboard(cfg, "rev", room, key)
      echo res
    of "append":
      if key.len == 0 or val.len == 0:
        stderr.writeLine("Usage: locutus blackboard append <room> <list_key> <entry> [--ttl <sec>]")
        quit(1)
      let res = doBlackboard(cfg, "append", room, key, resolveVal(val), ttlSec)
      echo res
    of "snapshot", "dump":
      let res = doBlackboard(cfg, "snapshot", room)
      if key.len > 0:
        try:
          writeFile(key, res)
          echo "OK"
        except CatchableError as e:
          stderr.writeLine("Error writing snapshot to file '" & key & "': " & e.msg)
          quit(1)
      else:
        echo res
    of "load", "restore":
      if key.len == 0:
        stderr.writeLine("Usage: locutus blackboard load <room> <file_or_json> [--ttl <sec>]")
        quit(1)
      var snapshotJson = key
      if fileExists(key):
        try:
          snapshotJson = readFile(key)
        except CatchableError as e:
          stderr.writeLine("Error reading snapshot file '" & key & "': " & e.msg)
          quit(1)
      elif key.startsWith("@") and fileExists(key[1..^1]):
        try:
          snapshotJson = readFile(key[1..^1])
        except CatchableError as e:
          stderr.writeLine("Error reading snapshot file '" & key[1..^1] & "': " & e.msg)
          quit(1)
      let res = doBlackboardLoad(cfg, room, snapshotJson, if ttlSec > 0: ttlSec else: 0)
      echo res
    of "delete", "del":
      if key.len == 0:
        stderr.writeLine("Usage: locutus blackboard delete <room> <key>")
        quit(1)
      let res = doBlackboard(cfg, "delete", room, key)
      echo res
    of "clear":
      let res = doBlackboard(cfg, "clear", room)
      echo res
    else:
      stderr.writeLine("Unknown blackboard action: " & action)
      stderr.writeLine("Usage: locutus blackboard <set|get|rev|append|snapshot|load|delete|clear> <room> [args...]")
      quit(1)

  of "floor":
    if args.len < 3:
      stderr.writeLine("Usage: locutus floor <request|yield|pass|status> <room> [args...]")
      quit(1)
    let action = args[1].toLowerAscii
    let room = args[2]
    var agentName = getActiveAgentName(cfg, "", fallbackDefault = false)

    case action
    of "request":
      var waitSec = 0
      var leaseSec = 60
      var i = 3
      while i < args.len:
        let a = args[i]
        if a.startsWith("--lease="):
          leaseSec = parseRequiredInt(a[8..^1], "--lease")
        elif a == "--lease" and i + 1 < args.len:
          leaseSec = parseRequiredInt(args[i+1], "--lease")
          inc i
        elif a.startsWith("--wait="):
          waitSec = parseRequiredInt(a[7..^1], "--wait")
        elif a == "--wait" and i + 1 < args.len:
          waitSec = parseRequiredInt(args[i+1], "--wait")
          inc i
        elif not a.startsWith("-"):
          waitSec = parseRequiredInt(a, "wait")
        inc i
      if agentName.len == 0:
        agentName = getActiveAgentName(cfg, "", fallbackDefault = false)
      if agentName.len == 0:
        stderr.writeLine("Error: No agent name specified. Run 'locutus open <name>', pass the agent name, or export LOCUTUS_AGENT_NAME=<name>.")
        quit(1)
      doFloorRequest(cfg, room, agentName, waitSec, leaseSec)

    of "yield":
      var force = false
      var leaseSec = 60
      var i = 3
      while i < args.len:
        let a = args[i]
        if a in ["--force", "-f"]: force = true
        elif a.startsWith("--lease="):
          leaseSec = parseRequiredInt(a[8..^1], "--lease")
        inc i
      if agentName.len == 0:
        agentName = getActiveAgentName(cfg, "", fallbackDefault = false)
      if agentName.len == 0:
        stderr.writeLine("Error: No agent name specified. Run 'locutus open <name>', pass the agent name, or export LOCUTUS_AGENT_NAME=<name>.")
        quit(1)
      doFloorYield(cfg, room, agentName, force, leaseSec)

    of "pass":
      var targetAgent = ""
      var force = false
      var leaseSec = 60
      var i = 3
      while i < args.len:
        let a = args[i]
        if a.startsWith("--to="): targetAgent = a[5..^1]
        elif a == "--to" and i + 1 < args.len: targetAgent = args[i+1]; inc i
        elif a in ["--force", "-f"]: force = true
        elif a.startsWith("--lease="):
          leaseSec = parseRequiredInt(a[8..^1], "--lease")
        elif not a.startsWith("-"):
          if targetAgent.len == 0: targetAgent = a
        inc i
      if targetAgent.len == 0:
        stderr.writeLine("Error: Missing target agent for floor pass. Use --to <agent>.")
        quit(1)
      if agentName.len == 0:
        agentName = getActiveAgentName(cfg, "", fallbackDefault = false)
      if agentName.len == 0:
        stderr.writeLine("Error: No agent name specified. Run 'locutus open <name>', pass the agent name, or export LOCUTUS_AGENT_NAME=<name>.")
        quit(1)
      doFloorPass(cfg, room, agentName, targetAgent, force, leaseSec)

    of "status", "show":
      doFloorStatus(cfg, room)

    else:
      stderr.writeLine("Unknown floor action: " & action)
      stderr.writeLine("Usage: locutus floor <request|yield|pass|status> <room> [args...]")
      quit(1)

  of "cancel":
    if args.len < 2:
      stderr.writeLine("Error: Missing run_id or cancel subcommand.")
      stderr.writeLine("Usage:")
      stderr.writeLine("  locutus cancel <run_id> [--reason <reason>] [--by <agent>] [--ttl <sec>]")
      stderr.writeLine("  locutus cancel check <run_id> [--raw] [--exit-code]")
      stderr.writeLine("  locutus cancel clear <run_id>")
      quit(1)

    var action = ""
    var runId = ""
    var reason = "Cancelled by orchestrator"
    var byAgent = getActiveAgentName(cfg, "", fallbackDefault = true)
    var ttlSec = 3600
    var rawOutput = false
    var exitCodeOnUncancelled = false

    if args[1] in ["check", "status"]:
      action = "check"
      if args.len > 2: runId = args[2]
      var i = 3
      while i < args.len:
        let a = args[i]
        if a in ["--raw", "-r"]: rawOutput = true
        elif a in ["--exit-code", "-e"]: exitCodeOnUncancelled = true
        elif not a.startsWith("-") and runId.len == 0: runId = a
        inc i
    elif args[1] in ["clear", "reset"]:
      action = "clear"
      if args.len > 2: runId = args[2]
      var i = 3
      while i < args.len:
        let a = args[i]
        if not a.startsWith("-") and runId.len == 0: runId = a
        inc i
    else:
      runId = args[1]
      var i = 2
      while i < args.len:
        let a = args[i]
        if a == "--check": action = "check"
        elif a == "--clear": action = "clear"
        elif a in ["--raw", "-r"]: rawOutput = true
        elif a in ["--exit-code", "-e"]: exitCodeOnUncancelled = true
        elif a.startsWith("--reason="): reason = a[9..^1]
        elif a == "--reason" and i + 1 < args.len: reason = args[i+1]; inc i
        elif a.startsWith("--by="): byAgent = a[5..^1]
        elif a == "--by" and i + 1 < args.len: byAgent = args[i+1]; inc i
        elif a.startsWith("--ttl="):
          ttlSec = parseRequiredInt(a[6..^1], "--ttl")
        elif a == "--ttl" and i + 1 < args.len:
          ttlSec = parseRequiredInt(args[i+1], "--ttl"); inc i
        elif not a.startsWith("-") and reason == "Cancelled by orchestrator":
          reason = a
        inc i

    if runId.len == 0:
      stderr.writeLine("Error: Missing run_id.")
      quit(1)

    case action
    of "check":
      doCancelCheck(cfg, runId, rawOutput, exitCodeOnUncancelled)
    of "clear":
      doCancelClear(cfg, runId)
    else:
      doCancelSet(cfg, runId, reason, byAgent, ttlSec)

  of "ballot":
    if args.len < 3:
      stderr.writeLine("Error: Missing ballot subcommand or ballot_id.")
      stderr.writeLine("Usage:")
      stderr.writeLine("  locutus ballot open <ballot_id> --options <opt1,opt2> [--voters <v1,v2>] [--ttl sec]")
      stderr.writeLine("  locutus ballot cast <ballot_id> --vote <choice> [--voter <agent>]")
      stderr.writeLine("  locutus ballot tally <ballot_id> [--close] [--raw]")
      stderr.writeLine("  locutus ballot status <ballot_id>")
      quit(1)

    let action = args[1].toLowerAscii
    let ballotId = args[2]

    case action
    of "open":
      var options = ""
      var voters = "*"
      var ttlSec = 3600
      var i = 3
      while i < args.len:
        let a = args[i]
        if a.startsWith("--options="): options = a[10..^1]
        elif a == "--options" and i + 1 < args.len: options = args[i+1]; inc i
        elif a.startsWith("--voters="): voters = a[9..^1]
        elif a == "--voters" and i + 1 < args.len: voters = args[i+1]; inc i
        elif a.startsWith("--ttl="):
          ttlSec = parseRequiredInt(a[6..^1], "--ttl")
        elif a == "--ttl" and i + 1 < args.len:
          ttlSec = parseRequiredInt(args[i+1], "--ttl"); inc i
        elif not a.startsWith("-") and options == "":
          options = a
        inc i
      if options.len == 0:
        stderr.writeLine("Error: Missing --options for ballot open.")
        quit(1)
      doBallotOpen(cfg, ballotId, options, voters, ttlSec)

    of "cast", "vote":
      var choice = ""
      var voter = ""
      var i = 3
      while i < args.len:
        let a = args[i]
        if a.startsWith("--vote="): choice = a[7..^1]
        elif a == "--vote" and i + 1 < args.len: choice = args[i+1]; inc i
        elif a.startsWith("--choice="): choice = a[9..^1]
        elif a == "--choice" and i + 1 < args.len: choice = args[i+1]; inc i
        elif a.startsWith("--voter="): voter = a[8..^1]
        elif a == "--voter" and i + 1 < args.len: voter = args[i+1]; inc i
        elif not a.startsWith("-") and choice == "":
          choice = a
        inc i
      if choice.len == 0:
        stderr.writeLine("Error: Missing --vote for ballot cast.")
        quit(1)
      if voter.len == 0:
        voter = getActiveAgentName(cfg, "", fallbackDefault = false)
      if voter.len == 0:
        stderr.writeLine("Error: No voter name specified. Pass --voter <name>, run 'locutus open <name>', or export LOCUTUS_AGENT_NAME=<name>.")
        quit(1)
      doBallotCast(cfg, ballotId, voter, choice)

    of "tally":
      var closeBallot = false
      var rawOutput = false
      var i = 3
      while i < args.len:
        let a = args[i]
        if a in ["--close", "-c"]: closeBallot = true
        elif a in ["--raw", "-r"]: rawOutput = true
        inc i
      doBallotTally(cfg, ballotId, closeBallot, rawOutput)

    of "status", "show":
      doBallotStatus(cfg, ballotId)

    else:
      stderr.writeLine("Unknown ballot action: " & action)
      stderr.writeLine("Usage: locutus ballot <open|cast|tally|status> <ballot_id> [args...]")
      quit(1)

  of "leader":
    if args.len < 3:
      stderr.writeLine("Error: Missing leader action or role name.")
      stderr.writeLine("Usage:")
      stderr.writeLine("  locutus leader acquire <role> [--lease <sec>] [--agent <name>]")
      stderr.writeLine("  locutus leader renew <role> [--lease <sec>] [--agent <name>]")
      stderr.writeLine("  locutus leader resign <role> [--agent <name>]")
      stderr.writeLine("  locutus leader status <role>")
      quit(1)

    let action = args[1].toLowerAscii
    let role = args[2]

    var agentName = ""
    var leaseSec = 30

    var i = 3
    while i < args.len:
      let a = args[i]
      if a.startsWith("--agent="): agentName = a[8..^1]
      elif a == "--agent" and i + 1 < args.len: agentName = args[i+1]; inc i
      elif a.startsWith("--lease="):
        leaseSec = parseRequiredInt(a[8..^1], "--lease")
      elif a == "--lease" and i + 1 < args.len:
        leaseSec = parseRequiredInt(args[i+1], "--lease"); inc i
      elif not a.startsWith("-"):
        leaseSec = parseRequiredInt(a, "lease")
      inc i

    if agentName.len == 0:
      agentName = getActiveAgentName(cfg, "", fallbackDefault = false)
    if agentName.len == 0 and action in ["acquire", "elect", "renew", "heartbeat", "resign", "release", "yield"]:
      stderr.writeLine("Error: No agent name specified. Pass --agent <name>, run 'locutus open <name>', or export LOCUTUS_AGENT_NAME=<name>.")
      quit(1)

    case action
    of "acquire", "elect":
      doLeaderAcquire(cfg, role, agentName, leaseSec)
    of "renew", "heartbeat":
      doLeaderRenew(cfg, role, agentName, leaseSec)
    of "resign", "release", "yield":
      doLeaderResign(cfg, role, agentName)
    of "status", "show":
      doLeaderStatus(cfg, role)
    else:
      stderr.writeLine("Unknown leader action: " & action)
      stderr.writeLine("Usage: locutus leader <acquire|renew|resign|status> <role> [args...]")
      quit(1)

  of "workflow", "dag":
    if args.len < 3:
      stderr.writeLine("Error: Missing workflow action or flow ID.")
      stderr.writeLine("Usage:")
      stderr.writeLine("  locutus workflow define <flow_id> --steps <s1,s2,...> [--deps <c:p1,p2;...>] [--ttl <sec>]")
      stderr.writeLine("  locutus workflow next <flow_id> [--raw]")
      stderr.writeLine("  locutus workflow resolve <flow_id> <step> [--output <msg>] [--raw]")
      stderr.writeLine("  locutus workflow fail <flow_id> <step> [--reason <msg>]")
      stderr.writeLine("  locutus workflow status <flow_id> [--raw]")
      stderr.writeLine("  locutus workflow export <flow_id> [output_file]")
      stderr.writeLine("  locutus workflow import <flow_id> <file_or_json> [--ttl <sec>]")
      quit(1)

    let action = args[1].toLowerAscii
    let flowId = args[2]

    var steps = ""
    var deps = ""
    var ttlSec = 86400
    var stepName = ""
    var outputMsg = ""
    var reasonMsg = ""
    var rawOutput = false

    var posArgs: seq[string] = @[]
    var i = 3
    while i < args.len:
      let a = args[i]
      if a.startsWith("--steps="): steps = a[8..^1]
      elif a == "--steps" and i + 1 < args.len: steps = args[i+1]; inc i
      elif a.startsWith("--deps="): deps = a[7..^1]
      elif a == "--deps" and i + 1 < args.len: deps = args[i+1]; inc i
      elif a.startsWith("--ttl="):
        ttlSec = parseRequiredInt(a[6..^1], "--ttl")
      elif a == "--ttl" and i + 1 < args.len:
        ttlSec = parseRequiredInt(args[i+1], "--ttl"); inc i
      elif a.startsWith("--output="): outputMsg = a[9..^1]
      elif a == "--output" and i + 1 < args.len: outputMsg = args[i+1]; inc i
      elif a.startsWith("--reason="): reasonMsg = a[9..^1]
      elif a == "--reason" and i + 1 < args.len: reasonMsg = args[i+1]; inc i
      elif a == "--raw": rawOutput = true
      elif not a.startsWith("-"):
        posArgs.add(a)
      inc i

    case action
    of "define", "create":
      doWorkflowDefine(cfg, flowId, steps, deps, ttlSec)
    of "next", "ready":
      doWorkflowNext(cfg, flowId, rawOutput)
    of "resolve", "complete":
      if posArgs.len > 0: stepName = posArgs[0]
      if stepName.len == 0:
        stderr.writeLine("Error: Missing step name to resolve.")
        quit(1)
      doWorkflowResolve(cfg, flowId, stepName, outputMsg, rawOutput)
    of "fail":
      if posArgs.len > 0: stepName = posArgs[0]
      if stepName.len == 0:
        stderr.writeLine("Error: Missing step name to fail.")
        quit(1)
      doWorkflowFail(cfg, flowId, stepName, reasonMsg)
    of "status", "show":
      doWorkflowStatus(cfg, flowId, rawOutput)
    of "export", "dump":
      let outPath = if posArgs.len > 0: posArgs[0] else: ""
      doWorkflowExport(cfg, flowId, outPath)
    of "import", "load":
      let inPayload = if posArgs.len > 0: posArgs[0] else: ""
      if inPayload.len == 0:
        stderr.writeLine("Error: Missing workflow payload or file to import.")
        stderr.writeLine("Usage: locutus workflow import <flow_id> <file_or_json> [--ttl <sec>]")
        quit(1)
      doWorkflowImport(cfg, flowId, inPayload, ttlSec)
    else:
      stderr.writeLine("Unknown workflow action: " & action)
      stderr.writeLine("Usage: locutus workflow <define|next|resolve|fail|status|export|import> <flow_id> [args...]")
      quit(1)

  of "sweep":
    var dryRun = false
    var rawOutput = false
    var i = 1
    while i < args.len:
      let a = args[i]
      if a == "--dry-run" or a == "-n": dryRun = true
      elif a == "--raw": rawOutput = true
      inc i
    doSweep(cfg, dryRun, rawOutput)

  of "status":
    var state = ""
    var activity = ""
    var explicitName = ""
    var rearmListen = false
    var listenTimeout = -1
    var i = 1
    while i < args.len:
      let a = args[i]
      if a in ["--listen", "-l"]:
        rearmListen = true
      elif a.startsWith("--listen="):
        rearmListen = true
        try: listenTimeout = parseInt(a[9..^1])
        except ValueError:
          stderr.writeLine("Error: Invalid integer for --listen: '" & a[9..^1] & "'")
          quit(1)
      elif a.startsWith("--listen-timeout="):
        rearmListen = true
        try: listenTimeout = parseInt(a[17..^1])
        except ValueError:
          stderr.writeLine("Error: Invalid integer for --listen-timeout: '" & a[17..^1] & "'")
          quit(1)
      elif a == "--listen-timeout" and i + 1 < args.len:
        rearmListen = true
        try: listenTimeout = parseInt(args[i+1])
        except ValueError:
          stderr.writeLine("Error: Invalid integer for --listen-timeout: '" & args[i+1] & "'")
          quit(1)
        inc i
      elif not a.startsWith("-"):
        if state.len == 0: state = a
        elif activity.len == 0: activity = a
        elif explicitName.len == 0: explicitName = a
      inc i

    if state.len == 0:
      stderr.writeLine("Error: Missing state argument for status command.")
      stderr.writeLine("Usage: locutus status <idle|busy|error> [activity_text] [name] [--listen/-l]")
      quit(1)

    let name = getActiveAgentName(cfg, explicitName, fallbackDefault = false)
    if name.len == 0:
      stderr.writeLine("Error: No agent name specified. Run 'locutus open <name>', pass the agent name, or export LOCUTUS_AGENT_NAME=<name>.")
      quit(1)

    let res = doStatus(cfg, name, state, activity)
    if not rearmListen:
      echo res
    else:
      stderr.writeLine("[LOCUTUS STATUS] " & res)
      let (alreadyListening, existingPid, existingHost) = getActiveListenerInfo(cfg, name)
      if alreadyListening:
        stderr.writeLine("[LOCUTUS LISTENER] Listener already active for agent '" & name & "' (PID " & $existingPid & " on " & existingHost & "). Skipping duplicate listener.")
      else:
        stderr.writeLine("[LOCUTUS LISTENER] Entering listening mode for agent '" & name & "'...")
        doListen(cfg, name, listenTimeout)

  of "lock":
    if args.len < 2:
      stderr.writeLine("Error: Missing lock name.")
      stderr.writeLine("Usage: locutus lock <lock_name> [ttl_sec] [--fencing] [--raw]")
      quit(1)
    let lockName = args[1]
    var ttl = 30
    var withFencing = false
    var rawOutput = false
    var i = 2
    while i < args.len:
      let a = args[i]
      if a == "--fencing" or a == "-f": withFencing = true
      elif a == "--raw": rawOutput = true
      elif not a.startsWith("-"):
        ttl = parseRequiredInt(a, "lock ttl")
      inc i
    let (msg, code) = doLock(cfg, lockName, ttl, withFencing, rawOutput)
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
      timeout = parseRequiredInt(args[2], "sub timeout")
    doSub(cfg, channel, timeout)

  else:
    stderr.writeLine("Unknown subcommand: " & subcmd)
    quit(1)

when isMainModule:
  main()
