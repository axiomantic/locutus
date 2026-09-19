# tests/test_resp.nim
# Rigorous unit tests for src/resp.nim (TASK-16 and TASK-17)

import std/[unittest, net, strutils, os]
import ../src/resp

suite "RESP Protocol Parser and Buffer Tests":
  test "TASK-16: Multi-kilobyte payload parsing with 8KB buffer":
    let server = newSocket()
    server.bindAddr(Port(0), "127.0.0.1")
    server.listen()
    let (_, port) = server.getLocalAddr()

    var thr: Thread[Socket]
    proc serveData(s: Socket) {.thread.} =
      var client: Socket = newSocket()
      s.accept(client)
      let data = repeat("A", 65536)
      let respData = "$" & $data.len & "\r\n" & data & "\r\n"
      client.send(respData)
      client.close()

    createThread(thr, serveData, server)

    let client = newRedisClient("redis://127.0.0.1:" & $int(port))
    client.connect()
    defer:
      client.close()
      server.close()

    let val = client.parseResp()
    check val.kind == rkBulkString
    check val.strVal.len == 65536
    check val.strVal == repeat("A", 65536)
    joinThread(thr)

  test "Empty bulk string ($0) parsing":
    let server = newSocket()
    server.bindAddr(Port(0), "127.0.0.1")
    server.listen()
    let (_, port) = server.getLocalAddr()

    var thr: Thread[Socket]
    proc serveEmpty(s: Socket) {.thread.} =
      var client: Socket = newSocket()
      s.accept(client)
      client.send("$0\r\n\r\n")
      client.close()

    createThread(thr, serveEmpty, server)

    let client = newRedisClient("redis://127.0.0.1:" & $int(port))
    client.connect()
    defer:
      client.close()
      server.close()

    let val = client.parseResp()
    check val.kind == rkBulkString
    check val.strVal == ""
    joinThread(thr)

  test "TASK-17: Maximum payload allocation guard (rejects > 32MB)":
    let server = newSocket()
    server.bindAddr(Port(0), "127.0.0.1")
    server.listen()
    let (_, port) = server.getLocalAddr()

    var thr: Thread[Socket]
    proc serveOversized(s: Socket) {.thread.} =
      var client: Socket = newSocket()
      s.accept(client)
      # Send a header claiming 40MB
      let respData = "$41943040\r\n"
      client.send(respData)
      client.close()

    createThread(thr, serveOversized, server)

    let client = newRedisClient("redis://127.0.0.1:" & $int(port))
    client.connect()
    defer:
      client.close()
      server.close()

    expect IOError:
      discard client.parseResp()

    joinThread(thr)
