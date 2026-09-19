# src/resp.nim
# Lightweight, pure-Nim RESP2 socket client for Redis.
# Zero third-party dependencies, uses only std/net, std/uri, and std/strutils.

import std/[net, uri, strutils, parseutils, os, osproc, streams]


type
  RespKind* = enum
    rkSimpleString,
    rkError,
    rkInteger,
    rkBulkString,
    rkNil,
    rkArray

  RespValue* = object
    case kind*: RespKind
    of rkSimpleString, rkError, rkBulkString:
      strVal*: string
    of rkInteger:
      intVal*: int64
    of rkNil:
      discard
    of rkArray:
      arrVal*: seq[RespValue]

  RedisClient* = ref object
    socket: Socket
    host*: string
    port*: int
    password*: string
    db*: int
    connected*: bool

proc `$`*(val: RespValue): string =
  case val.kind
  of rkSimpleString, rkBulkString:
    val.strVal
  of rkError:
    "Error: " & val.strVal
  of rkInteger:
    $val.intVal
  of rkNil:
    "(nil)"
  of rkArray:
    var parts: seq[string] = @[]
    for item in val.arrVal:
      parts.add($item)
    parts.join("\n")

proc parseRedisUrl*(rawUrl: string): tuple[host: string, port: int, password: string, db: int] =
  result.host = "127.0.0.1"
  result.port = 6379
  result.password = ""
  result.db = 0

  if rawUrl.len == 0:
    return

  let clean = if rawUrl.startsWith("redis://"): rawUrl else: "redis://" & rawUrl
  let u = parseUri(clean)
  if u.hostname.len > 0:
    result.host = u.hostname
  if u.port.len > 0:
    try:
      result.port = parseInt(u.port)
    except ValueError:
      discard
  if u.password.len > 0:
    result.password = u.password
  if u.path.len > 1:
    try:
      result.db = parseInt(u.path[1..^1])
    except ValueError:
      discard

proc newRedisClient*(url: string = "redis://127.0.0.1:6379"): RedisClient =
  let (h, p, pw, db) = parseRedisUrl(url)
  result = RedisClient(
    socket: newSocket(),
    host: h,
    port: p,
    password: pw,
    db: db,
    connected: false
  )

proc readLineCrLf(s: Socket, timeoutMs: int = -1): string =
  result = ""
  var c = ' '
  while true:
    let n = s.recv(c.addr, 1, timeoutMs)
    if n <= 0:
      raise newException(IOError, "Redis socket connection closed unexpectedly")
    if c == '\r':
      let n2 = s.recv(c.addr, 1, timeoutMs)
      if n2 > 0 and c == '\n':
        break
      elif n2 > 0:
        result.add('\r')
        result.add(c)
    else:
      result.add(c)

proc readExact(s: Socket, count: int, timeoutMs: int = -1): string =
  result = newString(count)
  var total = 0
  while total < count:
    let n = s.recv(result[total].addr, count - total, timeoutMs)
    if n <= 0:
      raise newException(IOError, "Redis socket connection closed unexpectedly while reading bulk data")
    total += n

proc parseResp*(s: Socket, timeoutMs: int = -1): RespValue =
  var prefix = ' '
  let n = s.recv(prefix.addr, 1, timeoutMs)
  if n <= 0:
    raise newException(IOError, "Redis connection closed")

  case prefix
  of '+':
    let line = s.readLineCrLf(timeoutMs)
    RespValue(kind: rkSimpleString, strVal: line)
  of '-':
    let line = s.readLineCrLf(timeoutMs)
    RespValue(kind: rkError, strVal: line)
  of ':':
    let line = s.readLineCrLf(timeoutMs)
    var val: int64 = 0
    discard parseBiggestInt(line, val)
    RespValue(kind: rkInteger, intVal: val)
  of '$':
    let line = s.readLineCrLf(timeoutMs)
    var len = 0
    discard parseInt(line, len)
    if len < 0:
      RespValue(kind: rkNil)
    else:
      let data = s.readExact(len, timeoutMs)
      discard s.readLineCrLf(timeoutMs) # Consume trailing CRLF
      RespValue(kind: rkBulkString, strVal: data)
  of '*':
    let line = s.readLineCrLf(timeoutMs)
    var count = 0
    discard parseInt(line, count)
    if count < 0:
      RespValue(kind: rkNil)
    else:
      var items: seq[RespValue] = newSeq[RespValue](count)
      for i in 0 ..< count:
        items[i] = s.parseResp(timeoutMs)
      RespValue(kind: rkArray, arrVal: items)
  else:
    raise newException(ValueError, "Unknown RESP protocol prefix: " & $prefix)

proc encodeCommand*(cmdArgs: openArray[string]): string =
  result = "*" & $cmdArgs.len & "\r\n"
  for a in cmdArgs:
    result.add("$" & $a.len & "\r\n" & a & "\r\n")

proc connect*(c: RedisClient, timeoutMs: int = 5000) =
  if c.connected: return
  c.socket.connect(c.host, Port(c.port), timeoutMs)
  c.connected = true

  if c.password.len > 0:
    c.socket.send(encodeCommand(["AUTH", c.password]))
    let resp = c.socket.parseResp()
    if resp.kind == rkError:
      raise newException(ValueError, "Redis AUTH failed: " & resp.strVal)

  if c.db != 0:
    c.socket.send(encodeCommand(["SELECT", $c.db]))
    let resp = c.socket.parseResp()
    if resp.kind == rkError:
      raise newException(ValueError, "Redis SELECT failed: " & resp.strVal)

proc close*(c: RedisClient) =
  if c.connected:
    try: c.socket.close()
    except CatchableError: discard
    c.connected = false

proc sendCommand*(c: RedisClient, cmdArgs: openArray[string]): RespValue =
  if not c.connected:
    c.connect()
  c.socket.send(encodeCommand(cmdArgs))
  return c.socket.parseResp()

proc runLua*(c: RedisClient, scriptText, scriptSha: string, evalArgs: openArray[string]): (string, int) =
  var shaCmd: seq[string] = @["EVALSHA", scriptSha, "0"]
  for a in evalArgs: shaCmd.add(a)
  try:
    let shaResp = c.sendCommand(shaCmd)
    if shaResp.kind == rkError and "NOSCRIPT" in shaResp.strVal:
      var evalCmd: seq[string] = @["EVAL", scriptText, "0"]
      for a in evalArgs: evalCmd.add(a)
      let evalResp = c.sendCommand(evalCmd)
      if evalResp.kind == rkError:
        return (evalResp.strVal, 1)
      return ($evalResp, 0)
    elif shaResp.kind == rkError:
      return (shaResp.strVal, 1)
    return ($shaResp, 0)
  except CatchableError as e:
    return ("Redis connection error: " & e.msg, 1)

proc execRedisFallback*(redisUrl: string, cmdArgs: openArray[string]): (string, int) =
  var fullArgs: seq[string] = @["-u", redisUrl]
  for a in cmdArgs:
    fullArgs.add(a)

  if findExe("redis-cli").len > 0:
    var p = startProcess("redis-cli", args = fullArgs, options = {poUsePath, poStdErrToStdOut})
    if p.inputStream != nil: p.inputStream.close()
    var outStr = ""
    var line = ""
    while true:
      if p.outputStream.readLine(line):
        outStr.add(line); outStr.add("\n")
      elif not running(p): break
    while p.outputStream.readLine(line):
      outStr.add(line); outStr.add("\n")
    let exitCode = p.waitForExit()
    p.close()
    return (outStr, exitCode)
  elif findExe("docker").len > 0:
    let container = getEnv("LOCUTUS_CONTAINER", getEnv("A2A_CONTAINER", "locutus-redis"))
    var dockerArgs: seq[string] = @["exec", "-i", container, "redis-cli"]
    for a in fullArgs: dockerArgs.add(a)
    var p = startProcess("docker", args = dockerArgs, options = {poUsePath, poStdErrToStdOut})
    if p.inputStream != nil: p.inputStream.close()
    var outStr = ""
    var line = ""
    while true:
      if p.outputStream.readLine(line):
        outStr.add(line); outStr.add("\n")
      elif not running(p): break
    while p.outputStream.readLine(line):
      outStr.add(line); outStr.add("\n")
    let exitCode = p.waitForExit()
    p.close()
    return (outStr, exitCode)
  else:
    return ("Error: Neither Redis socket nor 'redis-cli'/'docker' was available.", 1)

proc execRedisAuto*(redisUrl: string, cmdArgs: openArray[string]): (string, int) =
  var cleanArgs: seq[string] = @[]
  for a in cmdArgs:
    if a != "--raw": cleanArgs.add(a)

  try:
    let client = newRedisClient(redisUrl)
    defer: client.close()
    let resp = client.sendCommand(cleanArgs)
    if resp.kind == rkError:
      return (resp.strVal, 1)
    elif resp.kind == rkNil:
      return ("(nil)", 0)
    elif resp.kind == rkArray and cleanArgs.len > 0 and cleanArgs[0].toUpperAscii in ["BRPOP", "BLPOP"] and resp.arrVal.len >= 2:
      return (resp.arrVal[0].strVal & "\n" & resp.arrVal[1].strVal, 0)
    return ($resp, 0)
  except CatchableError:
    return execRedisFallback(redisUrl, cmdArgs)

proc subscribeOne*(redisUrl, channel: string, timeoutSec: int = -1): (string, int) =
  try:
    let client = newRedisClient(redisUrl)
    defer: client.close()
    client.connect()
    client.socket.send(encodeCommand(["SUBSCRIBE", channel]))
    let confirm = client.socket.parseResp()
    if confirm.kind == rkError:
      return (confirm.strVal, 1)
    let timeoutMs = if timeoutSec > 0: timeoutSec * 1000 else: -1
    try:
      let msgResp = client.socket.parseResp(timeoutMs)
      if msgResp.kind == rkArray and msgResp.arrVal.len >= 3 and msgResp.arrVal[0].strVal == "message":
        return (msgResp.arrVal[2].strVal, 0)
      elif msgResp.kind == rkNil:
        return ("(nil)", 0)
      else:
        return ($msgResp, 0)
    except TimeoutError:
      return ("(nil)", 0)
  except CatchableError as e:
    return ("Redis error: " & e.msg, 1)

