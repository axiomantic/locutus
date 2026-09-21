# Package
version       = "0.1.2"
author        = "Axiomantic"
description   = "High Performance Inter-Assistant Redis Bus & Multi-Agent Coordination Mesh"
license       = "MIT"
srcDir        = "src"
bin           = @["locutus"]
binDir        = "bin"

# Dependencies
requires "nim >= 2.0.0"
requires "https://github.com/elijahr/redis.git#a9ce032da61508f5af655856459047bed21cf4d6"

task test, "Run test suite":
  exec "uv run pytest -q"
