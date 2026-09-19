# src/locutus.nim
# High-performance, single-binary inter-assistant communication bus over Redis.
# Embeds Lua scripts at compile time and utilizes EVALSHA caching with automatic EVAL fallback.

import std/[
  os, osproc, strutils, json, openssl, parseopt,
  times, random, streams
]

# OpenSSL C-bindings for native cryptographic operations
proc SHA1*(d: cstring, n: csize_t, md: pointer): pointer {.cdecl, importc: "SHA1", dynlib: DLLSSLName.}
proc HMAC*(evp_md: EVP_MD, key: pointer, key_len: cint, d: cstring, n: csize_t, md: pointer, md_len: ptr cuint): cstring {.cdecl, importc: "HMAC", dynlib: DLLSSLName.}
proc CRYPTO_memcmp*(a: pointer, b: pointer, len: csize_t): cint {.cdecl, importc: "CRYPTO_memcmp", dynlib: DLLSSLName.}
proc RAND_bytes*(buf: pointer, num: cint): cint {.cdecl, importc: "RAND_bytes", dynlib: DLLSSLName.}

# Compile-time embedded Lua scripts
const
  registerLua*   = staticRead("../scripts/register.lua")
  sendO2oLua*    = staticRead("../scripts/send_o2o.lua")
  multicastLua*  = staticRead("../scripts/multicast.lua")
  directoryLua*  = staticRead("../scripts/directory.lua")
  drainLua*      = staticRead("../scripts/drain.lua")
  tagLua*        = staticRead("../scripts/tag.lua")
  unregisterLua* = staticRead("../scripts/unregister.lua")

# Cryptographic Helpers
proc computeSha1*(text: string): string =
  var digest: array[20, uint8]
  discard SHA1(text.cstring, text.len.csize_t, digest[0].addr)
  result = newStringOfCap(40)
  for b in digest:
    result.add(toHex(b.int, 2).toLowerAscii)

# Precomputed SHA1 hashes for Redis EVALSHA caching
let
  registerSha*   = computeSha1(registerLua)
  sendO2oSha*    = computeSha1(sendO2oLua)
  multicastSha*  = computeSha1(multicastLua)
  directorySha*  = computeSha1(directoryLua)
  drainSha*      = computeSha1(drainLua)
  tagSha*        = computeSha1(tagLua)
  unregisterSha* = computeSha1(unregisterLua)

proc getSecret*(): string =
  let envSecret = getEnv("LOCUTUS_SECRET", "")
  if envSecret.len > 0:
    return envSecret
  let home = getHomeDir()
  let configDir = home / ".config" / "locutus"
  let secretFile = configDir / "secret"
  if fileExists(secretFile):
    return readFile(secretFile).strip()

  createDir(configDir)
  var bytes: array[32, uint8]
  discard RAND_bytes(bytes[0].addr, 32)
  var hexSecret = ""
  for b in bytes:
    hexSecret.add(toHex(b.int, 2).toLowerAscii)
  writeFile(secretFile, hexSecret)
  setFilePermissions(secretFile, {fpUserRead, fpUserWrite})
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

proc encryptAes*(plaintext, secret: string): string =
  var p = startProcess("openssl", args = ["enc", "-aes-256-cbc", "-pbkdf2", "-iter", "10000", "-salt", "-pass", "pass:" & secret, "-base64", "-A"], options = {poUsePath})
  p.inputStream.write(plaintext)
  p.inputStream.close()
  result = p.outputStream.readAll().strip()
  discard p.waitForExit()
  p.close()

proc decryptAes*(ciphertext, secret: string): string =
  var p = startProcess("openssl", args = ["enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "10000", "-salt", "-pass", "pass:" & secret, "-base64", "-A"], options = {poUsePath})
  p.inputStream.write(ciphertext)
  p.inputStream.close()
  result = p.outputStream.readAll().strip()
  discard p.waitForExit()
  p.close()

# Environment & Config Resolution
type LocutusConfig* = object
  redisUrl*: string
  prefix*: string
  project*: string
  encrypt*: bool

proc resolveConfig*(): LocutusConfig =
  var redisUrl = getEnv("LOCUTUS_REDIS_URL", "")
  var prefix = getEnv("LOCUTUS_REDIS_PREFIX", "")
  var project = getEnv("LOCUTUS_PROJECT", "")

  # Check AGENTS.md
  if fileExists("AGENTS.md"):
    for line in lines("AGENTS.md"):
      let low = line.toLowerAscii.strip()
      if redisUrl == "" and (low.startsWith("locutus_redis_url:") or low.startsWith("locutus_redis_url=")):
        redisUrl = line.split({':', '='}, maxsplit = 1)[1].strip(chars = {'"', '\'', ' '})
      if prefix == "" and (low.startsWith("locutus_redis_prefix:") or low.startsWith("locutus_redis_prefix=")):
        prefix = line.split({':', '='}, maxsplit = 1)[1].strip(chars = {'"', '\'', ' '})
      if project == "" and (low.startsWith("locutus_project:") or low.startsWith("locutus_project=")):
        project = line.split({':', '='}, maxsplit = 1)[1].strip(chars = {'"', '\'', ' '})

  # Check .env
  if fileExists(".env"):
    for line in lines(".env"):
      if redisUrl == "" and line.startsWith("LOCUTUS_REDIS_URL="):
        redisUrl = line.split('=', maxsplit = 1)[1].strip(chars = {'"', '\'', ' '})
      if prefix == "" and line.startsWith("LOCUTUS_REDIS_PREFIX="):
        prefix = line.split('=', maxsplit = 1)[1].strip(chars = {'"', '\'', ' '})
      if project == "" and line.startsWith("LOCUTUS_PROJECT="):
        project = line.split('=', maxsplit = 1)[1].strip(chars = {'"', '\'', ' '})

  # Fallbacks
  if redisUrl == "":
    redisUrl = getEnv("A2A_REDIS_URL", getEnv("REDIS_URL", "redis://127.0.0.1:6379"))
  if prefix == "":
    prefix = getEnv("A2A_REDIS_PREFIX", "locutus:")
  if project == "":
    project = getEnv("A2A_PROJECT", getCurrentDir().splitPath.tail)

  let enc = getEnv("LOCUTUS_ENCRYPT", "0") in ["1", "true", "TRUE"]
  return LocutusConfig(redisUrl: redisUrl, prefix: prefix, project: project, encrypt: enc)

# Redis CLI Execution with EVALSHA & Docker fallback
proc execRedis(redisUrl: string, cmdArgs: openArray[string]): (string, int) =
  var fullArgs: seq[string] = @["-u", redisUrl]
  for a in cmdArgs:
    fullArgs.add(a)

  # Check if redis-cli is present
  if findExe("redis-cli").len > 0:
    var p = startProcess("redis-cli", args = fullArgs, options = {poUsePath})
    let outStr = p.outputStream.readAll()
    let exitCode = p.waitForExit()
    p.close()
    return (outStr, exitCode)
  else:
    # Docker fallback
    let container = getEnv("LOCUTUS_CONTAINER", "a2a-redis")
    var dockerArgs: seq[string] = @["exec", "-i", container, "redis-cli"]
    for a in fullArgs:
      dockerArgs.add(a)
    var p = startProcess("docker", args = dockerArgs, options = {poUsePath})
    let outStr = p.outputStream.readAll()
    let exitCode = p.waitForExit()
    p.close()
    return (outStr, exitCode)

proc runLuaScript*(redisUrl, scriptText, scriptSha: string, evalArgs: openArray[string]): string =
  var shaArgs: seq[string] = @["EVALSHA", scriptSha, "0"]
  for a in evalArgs:
    shaArgs.add(a)
  let (shaOut, _) = execRedis(redisUrl, shaArgs)
  if "NOSCRIPT" in shaOut:
    # Fallback to EVAL and automatically cache the script in Redis
    var evalCmd: seq[string] = @["EVAL", scriptText, "0"]
    for a in evalArgs:
      evalCmd.add(a)
    let (evalOut, _) = execRedis(redisUrl, evalCmd)
    return evalOut.strip()
  return shaOut.strip()

# Core Operations
proc doRegister*(cfg: LocutusConfig, name, tags: string, ttl: int = 150): string =
  return runLuaScript(cfg.redisUrl, registerLua, registerSha, [cfg.prefix, name, tags, $ttl])

proc doDrain*(cfg: LocutusConfig, name: string, count: int = 50): string =
  return runLuaScript(cfg.redisUrl, drainLua, drainSha, [cfg.prefix, name, $count])

proc doUnregister*(cfg: LocutusConfig, name: string): string =
  return runLuaScript(cfg.redisUrl, unregisterLua, unregisterSha, [cfg.prefix, name])

proc doTag*(cfg: LocutusConfig, name, action, tags: string): string =
  return runLuaScript(cfg.redisUrl, tagLua, tagSha, [cfg.prefix, name, action, tags])

proc doDirectory*(cfg: LocutusConfig, filterTag: string = ""): string =
  return runLuaScript(cfg.redisUrl, directoryLua, directorySha, [cfg.prefix, filterTag])

proc doOpen*(cfg: LocutusConfig, optName, optTags: string) =
  randomize()
  let name = if optName.len > 0: optName else: cfg.project & "-worker-" & $rand(1000..9999)
  let tags = if optTags.len > 0:
    if optTags.startsWith(cfg.project): optTags else: cfg.project & "," & optTags
  else:
    cfg.project

  discard doRegister(cfg, name, tags, 150)
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

proc doSend*(cfg: LocutusConfig, toAgent, msgType, fromAgent, subject, body: string,
            tags: seq[string] = @[], replyTo: string = "", msgId: string = "", isBroadcast: bool = false) =
  randomize()
  let secret = getSecret()
  let id = if msgId.len > 0: msgId else: "msg_" & $getTime().toUnix() & "_" & fromAgent & "_" & $rand(1000..9999)
  let ts = now().utc.format("yyyy-MM-dd'T'HH:mm:ss'Z'")
  let finalBody = if cfg.encrypt: encryptAes(body, secret) else: body

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

  if isBroadcast:
    let target = if toAgent.len > 0: toAgent else: cfg.project
    let res = runLuaScript(cfg.redisUrl, multicastLua, multicastSha, [cfg.prefix, target, msgJson, "604800"])
    echo res
  else:
    let res = runLuaScript(cfg.redisUrl, sendO2oLua, sendO2oSha, [cfg.prefix, toAgent, msgJson, "604800"])
    echo res

proc doListen*(cfg: LocutusConfig, name: string, timeoutSec: int = 90) =
  let secret = getSecret()
  let inboxKey = cfg.prefix & "inbox:" & name

  # BRPOP blocks until message arrives or timeout expires
  var (outStr, exitCode) = execRedis(cfg.redisUrl, ["--raw", "BRPOP", inboxKey, $timeoutSec])
  if exitCode != 0 or outStr.strip().len == 0:
    echo "(nil)"
    return

  let lines = outStr.strip().splitLines()
  if lines.len < 2:
    echo "(nil)"
    return

  # In --raw BRPOP, line 1 is key name, lines 2+ is payload
  let payloadStr = lines[1..^1].join("\n").strip()

  var parsed: JsonNode
  try:
    parsed = parseJson(payloadStr)
  except JsonParsingError:
    stderr.writeLine("[LOCUTUS SECURITY] ⚠️ Dropping non-JSON payload from inbox")
    echo "(nil)"
    return

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
    stderr.writeLine("[LOCUTUS SECURITY] ⚠️ Dropping unauthenticated/tampered message (ID: " & id & ")")
    # Loop/exit with nil to maintain air-gap
    echo "(nil)"
    return

  # Authenticated! Decrypt if required
  if isEncrypted:
    let decryptedBody = decryptAes(body, secret)
    parsed["body"] = %decryptedBody
    parsed["encrypted"] = %false

  echo $parsed

# Main Entrypoint / CLI Router
proc main() =
  let cfg = resolveConfig()
  var args = commandLineParams()

  if args.len == 0 or args[0] in ["-h", "--help", "help"]:
    echo "Locutus - High Performance Inter-Assistant Redis Bus (Nim Native)"
    echo "Usage:"
    echo "  locutus open [name] [tags]"
    echo "  locutus listen [name] [timeout_sec]"
    echo "  locutus send --to <agent> [--type task|query|reply|status] --subject <subj> --body <body>"
    echo "  locutus broadcast [--tags <tags>] --subject <subj> --body <body>"
    echo "  locutus who [filter_tag]"
    echo "  locutus tag <add|remove|set> <tags> [name]"
    echo "  locutus drain [count] [name]"
    echo "  locutus close [name]"
    echo "  locutus get-secret"
    return

  let subcmd = args[0].toLowerAscii
  case subcmd
  of "open", "register":
    let name = if args.len > 1: args[1] else: ""
    let tags = if args.len > 2: args[2] else: ""
    doOpen(cfg, name, tags)

  of "listen":
    var name = ""
    var timeout = 90
    if args.len > 1:
      try:
        timeout = parseInt(args[1])
      except ValueError:
        name = args[1]
    if args.len > 2:
      try:
        timeout = parseInt(args[2])
      except ValueError:
        discard
    if name == "":
      name = cfg.project & "-worker"
    doListen(cfg, name, timeout)

  of "send", "broadcast":
    let isBroadcast = (subcmd == "broadcast")
    var toAgent = ""
    var msgType = "task"
    var fromAgent = cfg.project & "-worker"
    var subject = ""
    var body = ""
    var tags: seq[string] = @[]
    var replyTo = ""
    var msgId = ""

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
      elif a == "--reply-to" and i + 1 < args.len: replyTo = args[i+1]; inc i
      elif a.startsWith("--id="): msgId = a[5..^1]
      elif a == "--id" and i + 1 < args.len: msgId = args[i+1]; inc i
      elif not a.startsWith("-"):
        # Positional arguments fallback: <to> <subject> <body>
        if toAgent == "": toAgent = a
        elif subject == "": subject = a
        elif body == "": body = a
      inc i

    if isBroadcast and toAgent == "":
      toAgent = cfg.project

    doSend(cfg, toAgent, msgType, fromAgent, subject, body, tags, replyTo, msgId, isBroadcast)

  of "who":
    let filterTag = if args.len > 1: args[1] else: cfg.project
    echo doDirectory(cfg, filterTag)

  of "tag":
    if args.len < 3:
      echo "Usage: locutus tag <add|remove|set> <tags> [name]"
      return
    let action = args[1]
    let tags = args[2]
    let name = if args.len > 3: args[3] else: cfg.project & "-worker"
    echo doTag(cfg, name, action, tags)

  of "drain":
    let count = if args.len > 1: parseInt(args[1]) else: 50
    let name = if args.len > 2: args[2] else: cfg.project & "-worker"
    echo doDrain(cfg, name, count)

  of "close", "unregister":
    let name = if args.len > 1: args[1] else: cfg.project & "-worker"
    echo doUnregister(cfg, name)

  of "get-secret":
    echo getSecret()

  else:
    stderr.writeLine("Unknown subcommand: " & subcmd)
    quit(1)

when isMainModule:
  main()
