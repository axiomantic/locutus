# src/config.nim
# Tiered configuration engine for Locutus.
# Handles cascading resolution across CLI flags, env vars, workspace TOML,
# user config, system config, defaults, and named profiles.

import std/[os, strutils, tables, json, options]

type
  SettingSource* = enum
    srcDefault,
    srcSystemFile,
    srcUserFile,
    srcWorkspaceFile,
    srcCustomFile,
    srcEnv,
    srcCli

  ProvenanceEntry* = object
    key*: string
    value*: string
    source*: SettingSource
    detail*: string

  LocutusConfig* = object
    redisUrl*: string
    prefix*: string
    project*: string
    agentName*: string
    secret*: string
    secretFile*: string
    encrypt*: bool
    cluster*: bool
    heartbeatTtl*: int
    messageTtl*: int
    listenTimeout*: int
    profile*: string
    activeConfigFile*: string
    provenance*: Table[string, ProvenanceEntry]

proc sourceLabel*(s: SettingSource): string =
  case s
  of srcDefault: "default"
  of srcSystemFile: "system config"
  of srcUserFile: "user config"
  of srcWorkspaceFile: "workspace config"
  of srcCustomFile: "custom file"
  of srcEnv: "environment"
  of srcCli: "cli flag"

# Strip surrounding single or double quotes
proc unquote(s: string): string =
  let t = s.strip()
  if (t.startsWith("\"") and t.endsWith("\"")) or (t.startsWith("'") and t.endsWith("'")):
    if t.len >= 2:
      return t[1 .. ^2]
  return t

# Expand tilde in path
proc expandPathSafe*(p: string): string =
  if p.startsWith("~" & $DirSep) or p.startsWith("~/") or p == "~":
    let home = getHomeDir()
    let rest = p[1..^1].strip(chars = {'/', '\\', DirSep})
    if rest.len == 0:
      return normalizedPath(home)
    return normalizedPath(home / rest)
  return p

# Standard configuration paths
proc getSystemConfigPath*(): string =
  when defined(windows):
    return getEnv("ProgramData", r"C:\ProgramData") / "locutus" / "config.toml"
  elif defined(macosx):
    return "/Library/Application Support/locutus/config.toml"
  else:
    return "/etc/locutus/config.toml"

proc getUserConfigPath*(): string =
  when defined(windows):
    return getEnv("APPDATA", getHomeDir() / "AppData" / "Roaming") / "locutus" / "config.toml"
  elif defined(macosx):
    let appSupp = getHomeDir() / "Library" / "Application Support" / "locutus" / "config.toml"
    if fileExists(appSupp): return appSupp
    return getEnv("XDG_CONFIG_HOME", getHomeDir() / ".config") / "locutus" / "config.toml"
  else:
    return getEnv("XDG_CONFIG_HOME", getHomeDir() / ".config") / "locutus" / "config.toml"

# Walk up from current directory to find workspace configuration or git root
proc findWorkspaceConfigPath*(startDir: string = getCurrentDir()): string =
  var cur = startDir
  while true:
    for candidate in [".locutus.toml", "locutus.toml", ".locutus.json", ".env", "AGENTS.md"]:
      let p = cur / candidate
      if fileExists(p):
        return p
    # Check if .git directory exists here; stop walking up past git root
    if dirExists(cur / ".git") or fileExists(cur / ".git"):
      break
    let parent = cur.parentDir()
    if parent == cur or parent.len == 0:
      break
    cur = parent
  return ""

# Lightweight TOML parser supporting sections, key-value strings, ints, bools
type TomlTable = Table[string, Table[string, string]]

proc parseSimpleToml*(content: string): TomlTable =
  result = initTable[string, Table[string, string]]()
  result[""] = initTable[string, string]()
  var curSection = ""

  for rawLine in content.splitLines():
    var line = rawLine.strip()
    if line.len == 0 or line.startsWith("#") or line.startsWith(";"):
      continue

    # Remove inline comments if not inside quotes
    var inQuotes = false
    var cleanLine = ""
    for c in line:
      if c == '"' or c == '\'': inQuotes = not inQuotes
      elif c == '#' and not inQuotes: break
      cleanLine.add(c)
    cleanLine = cleanLine.strip()
    if cleanLine.len == 0: continue

    # Section header: [section] or [profiles.name]
    if cleanLine.startsWith("[") and cleanLine.endsWith("]"):
      curSection = cleanLine[1 .. ^2].strip().toLowerAscii
      if not result.hasKey(curSection):
        result[curSection] = initTable[string, string]()
      continue

    # Key = Value
    let eqIdx = cleanLine.find('=')
    if eqIdx > 0:
      let key = cleanLine[0 ..< eqIdx].strip().toLowerAscii
      let val = cleanLine[eqIdx + 1 .. ^1].strip().unquote()
      if not result.hasKey(curSection):
        result[curSection] = initTable[string, string]()
      result[curSection][key] = val

proc parseSimpleEnv*(content: string): Table[string, string] =
  result = initTable[string, string]()
  for line in content.splitLines():
    let s = line.strip()
    if s.len == 0 or s.startsWith("#"): continue
    let eqIdx = s.find('=')
    if eqIdx > 0:
      let k = s[0 ..< eqIdx].strip()
      let v = s[eqIdx + 1 .. ^1].strip().unquote()
      result[k] = v

proc parseAgentsMd*(content: string): Table[string, string] =
  result = initTable[string, string]()
  for line in content.splitLines():
    let low = line.toLowerAscii.strip()
    for prefix in ["locutus_redis_url", "locutus_redis_prefix", "locutus_project", "locutus_agent_name"]:
      if low.startsWith(prefix & ":") or low.startsWith(prefix & "="):
        let parts = line.split({':', '='}, maxsplit = 1)
        if parts.len >= 2:
          result[prefix] = parts[1].strip().unquote()

# Populate config from a dictionary with provenance
proc applyDict(
  cfg: var LocutusConfig,
  dict: Table[string, string],
  source: SettingSource,
  detail: string
) =
  for k, v in dict:
    let lowKey = k.toLowerAscii.replace("-", "_")
    case lowKey
    of "redis_url", "redisurl", "locutus_redis_url":
      if v.len > 0:
        cfg.redisUrl = v
        cfg.provenance["redis_url"] = ProvenanceEntry(key: "redis_url", value: v, source: source, detail: detail)
    of "prefix", "redis_prefix", "locutus_redis_prefix":
      if v.len > 0:
        cfg.prefix = v
        cfg.provenance["prefix"] = ProvenanceEntry(key: "prefix", value: v, source: source, detail: detail)
    of "project", "locutus_project":
      if v.len > 0:
        cfg.project = v
        cfg.provenance["project"] = ProvenanceEntry(key: "project", value: v, source: source, detail: detail)
    of "agent_name", "agentname", "locutus_agent_name":
      if v.len > 0:
        cfg.agentName = v
        cfg.provenance["agent_name"] = ProvenanceEntry(key: "agent_name", value: v, source: source, detail: detail)
    of "secret", "locutus_secret":
      if v.len > 0:
        cfg.secret = v
        cfg.provenance["secret"] = ProvenanceEntry(key: "secret", value: "[REDACTED]", source: source, detail: detail)
    of "secret_file", "secretfile", "locutus_secret_file":
      if v.len > 0:
        cfg.secretFile = expandPathSafe(v)
        cfg.provenance["secret_file"] = ProvenanceEntry(key: "secret_file", value: cfg.secretFile, source: source, detail: detail)
    of "encrypt", "locutus_encrypt":
      let b = v.toLowerAscii in ["1", "true", "yes", "on"]
      cfg.encrypt = b
      cfg.provenance["encrypt"] = ProvenanceEntry(key: "encrypt", value: $b, source: source, detail: detail)
    of "cluster", "locutus_cluster", "locutus_redis_cluster":
      let b = v.toLowerAscii in ["1", "true", "yes", "on"]
      cfg.cluster = b
      cfg.provenance["cluster"] = ProvenanceEntry(key: "cluster", value: $b, source: source, detail: detail)
    of "heartbeat_ttl", "heartbeat":
      try:
        let n = parseInt(v)
        cfg.heartbeatTtl = n
        cfg.provenance["heartbeat_ttl"] = ProvenanceEntry(key: "heartbeat_ttl", value: $n, source: source, detail: detail)
      except ValueError: discard
    of "message_ttl", "ttl":
      try:
        let n = parseInt(v)
        cfg.messageTtl = n
        cfg.provenance["message_ttl"] = ProvenanceEntry(key: "message_ttl", value: $n, source: source, detail: detail)
      except ValueError: discard
    of "listen_timeout", "timeout":
      try:
        let n = parseInt(v)
        cfg.listenTimeout = n
        cfg.provenance["listen_timeout"] = ProvenanceEntry(key: "listen_timeout", value: $n, source: source, detail: detail)
      except ValueError: discard
    else:
      discard

# Load file into config with profile support
proc loadConfigFile(
  cfg: var LocutusConfig,
  path: string,
  source: SettingSource,
  targetProfile: string = ""
) =
  if not fileExists(path): return
  cfg.activeConfigFile = path

  let ext = path.splitFile.ext.toLowerAscii
  if ext in [".toml"]:
    let toml = parseSimpleToml(readFile(path))
    # Apply root section
    if toml.hasKey(""):
      applyDict(cfg, toml[""], source, path)
    # Apply [default] section if present
    if toml.hasKey("default"):
      applyDict(cfg, toml["default"], source, path & " [default]")
    # Apply requested profile if specified
    if targetProfile.len > 0:
      let profKey1 = "profiles." & targetProfile.toLowerAscii
      let profKey2 = targetProfile.toLowerAscii
      if toml.hasKey(profKey1):
        applyDict(cfg, toml[profKey1], source, path & " [" & profKey1 & "]")
      elif toml.hasKey(profKey2):
        applyDict(cfg, toml[profKey2], source, path & " [" & profKey2 & "]")
  elif ext in [".json"]:
    try:
      let j = parseJson(readFile(path))
      var dict = initTable[string, string]()
      for k, v in j.pairs:
        if v.kind == JString: dict[k] = v.getStr()
        elif v.kind in [JInt, JBool]: dict[k] = $v
      applyDict(cfg, dict, source, path)

      if targetProfile.len > 0 and j.hasKey("profiles") and j["profiles"].hasKey(targetProfile):
        var profDict = initTable[string, string]()
        for k, v in j["profiles"][targetProfile].pairs:
          if v.kind == JString: profDict[k] = v.getStr()
          elif v.kind in [JInt, JBool]: profDict[k] = $v
        applyDict(cfg, profDict, source, path & " [profiles." & targetProfile & "]")
    except CatchableError:
      discard
  elif path.endsWith(".env"):
    let envDict = parseSimpleEnv(readFile(path))
    applyDict(cfg, envDict, source, path)
  elif path.endsWith("AGENTS.md"):
    let agentsDict = parseAgentsMd(readFile(path))
    applyDict(cfg, agentsDict, source, path)

# The Full Cascading Resolution Protocol
type CliOverrides* = object
  redisUrl*: string
  prefix*: string
  project*: string
  agentName*: string
  secret*: string
  secretFile*: string
  encrypt*: Option[bool]
  cluster*: Option[bool]
  timeout*: Option[int]
  profile*: string
  configFile*: string

proc resolveFullConfig*(cli: CliOverrides = CliOverrides()): LocutusConfig =
  # Step 1: Initialize with default values
  let defProject = getCurrentDir().splitPath.tail
  result = LocutusConfig(
    redisUrl: "redis://127.0.0.1:6379",
    prefix: "locutus:",
    project: defProject,
    agentName: defProject & "-worker",
    secret: "",
    secretFile: "",
    encrypt: false,
    cluster: false,
    heartbeatTtl: 150,
    messageTtl: 604800,
    listenTimeout: 90,
    profile: "default",
    provenance: initTable[string, ProvenanceEntry]()
  )

  # Record initial defaults in provenance
  result.provenance["redis_url"] = ProvenanceEntry(key: "redis_url", value: result.redisUrl, source: srcDefault, detail: "builtin default")
  result.provenance["prefix"] = ProvenanceEntry(key: "prefix", value: result.prefix, source: srcDefault, detail: "builtin default")
  result.provenance["project"] = ProvenanceEntry(key: "project", value: result.project, source: srcDefault, detail: "current directory basename")
  result.provenance["agent_name"] = ProvenanceEntry(key: "agent_name", value: result.agentName, source: srcDefault, detail: "${project}-worker")
  result.provenance["encrypt"] = ProvenanceEntry(key: "encrypt", value: "false", source: srcDefault, detail: "builtin default")
  result.provenance["cluster"] = ProvenanceEntry(key: "cluster", value: "false", source: srcDefault, detail: "builtin default")
  result.provenance["heartbeat_ttl"] = ProvenanceEntry(key: "heartbeat_ttl", value: $result.heartbeatTtl, source: srcDefault, detail: "150s default")
  result.provenance["message_ttl"] = ProvenanceEntry(key: "message_ttl", value: $result.messageTtl, source: srcDefault, detail: "7 days default")
  result.provenance["listen_timeout"] = ProvenanceEntry(key: "listen_timeout", value: $result.listenTimeout, source: srcDefault, detail: "90s default")

  # Resolve profile selector
  var targetProfile = cli.profile
  if targetProfile.len == 0:
    targetProfile = getEnv("LOCUTUS_PROFILE", "")
  if targetProfile.len > 0:
    result.profile = targetProfile

  # Step 2: Global / System Configuration
  let sysPath = getSystemConfigPath()
  if fileExists(sysPath):
    loadConfigFile(result, sysPath, srcSystemFile, targetProfile)

  # Step 3: Per-User Configuration
  let userPath = getUserConfigPath()
  if fileExists(userPath):
    loadConfigFile(result, userPath, srcUserFile, targetProfile)

  # Step 4: Workspace / Project Configuration
  let wsPath = findWorkspaceConfigPath()
  if wsPath.len > 0 and fileExists(wsPath):
    loadConfigFile(result, wsPath, srcWorkspaceFile, targetProfile)

  # Step 5: Custom Config File Override (if provided via CLI or LOCUTUS_CONFIG)
  var customPath = cli.configFile
  if customPath.len == 0:
    customPath = getEnv("LOCUTUS_CONFIG", "")
  if customPath.len > 0:
    customPath = expandPathSafe(customPath)
    if fileExists(customPath):
      loadConfigFile(result, customPath, srcCustomFile, targetProfile)
    else:
      stderr.writeLine("Warning: Specified configuration file does not exist: " & customPath)

  # Step 6: Process Environment Variables
  let envRedisUrl = getEnv("LOCUTUS_REDIS_URL", getEnv("A2A_REDIS_URL", getEnv("REDIS_URL", "")))
  if envRedisUrl.len > 0:
    result.redisUrl = envRedisUrl
    result.provenance["redis_url"] = ProvenanceEntry(key: "redis_url", value: envRedisUrl, source: srcEnv, detail: "LOCUTUS_REDIS_URL / REDIS_URL")

  let envPrefix = getEnv("LOCUTUS_REDIS_PREFIX", getEnv("A2A_REDIS_PREFIX", ""))
  if envPrefix.len > 0:
    result.prefix = envPrefix
    result.provenance["prefix"] = ProvenanceEntry(key: "prefix", value: envPrefix, source: srcEnv, detail: "LOCUTUS_REDIS_PREFIX")

  let envProject = getEnv("LOCUTUS_PROJECT", getEnv("A2A_PROJECT", ""))
  if envProject.len > 0:
    result.project = envProject
    result.provenance["project"] = ProvenanceEntry(key: "project", value: envProject, source: srcEnv, detail: "LOCUTUS_PROJECT")

  let envAgent = getEnv("LOCUTUS_AGENT_NAME", getEnv("MY_NAME", ""))
  if envAgent.len > 0:
    result.agentName = envAgent
    result.provenance["agent_name"] = ProvenanceEntry(key: "agent_name", value: envAgent, source: srcEnv, detail: "LOCUTUS_AGENT_NAME")

  let envSecret = getEnv("LOCUTUS_SECRET", "")
  if envSecret.len > 0:
    result.secret = envSecret
    result.provenance["secret"] = ProvenanceEntry(key: "secret", value: "[REDACTED]", source: srcEnv, detail: "LOCUTUS_SECRET")

  let envSecretFile = getEnv("LOCUTUS_SECRET_FILE", "")
  if envSecretFile.len > 0:
    result.secretFile = expandPathSafe(envSecretFile)
    result.provenance["secret_file"] = ProvenanceEntry(key: "secret_file", value: result.secretFile, source: srcEnv, detail: "LOCUTUS_SECRET_FILE")

  let envEnc = getEnv("LOCUTUS_ENCRYPT", "")
  if envEnc.len > 0:
    let b = envEnc in ["1", "true", "TRUE", "yes"]
    result.encrypt = b
    result.provenance["encrypt"] = ProvenanceEntry(key: "encrypt", value: $b, source: srcEnv, detail: "LOCUTUS_ENCRYPT=" & envEnc)

  let envCluster = getEnv("LOCUTUS_CLUSTER", getEnv("LOCUTUS_REDIS_CLUSTER", ""))
  if envCluster.len > 0:
    let b = envCluster in ["1", "true", "TRUE", "yes"]
    result.cluster = b
    result.provenance["cluster"] = ProvenanceEntry(key: "cluster", value: $b, source: srcEnv, detail: "LOCUTUS_CLUSTER=" & envCluster)

  # Step 7: Explicit CLI Arguments
  if cli.redisUrl.len > 0:
    result.redisUrl = cli.redisUrl
    result.provenance["redis_url"] = ProvenanceEntry(key: "redis_url", value: cli.redisUrl, source: srcCli, detail: "--redis-url")

  if cli.prefix.len > 0:
    result.prefix = cli.prefix
    result.provenance["prefix"] = ProvenanceEntry(key: "prefix", value: cli.prefix, source: srcCli, detail: "--prefix")

  if cli.project.len > 0:
    result.project = cli.project
    result.provenance["project"] = ProvenanceEntry(key: "project", value: cli.project, source: srcCli, detail: "--project")

  if cli.agentName.len > 0:
    result.agentName = cli.agentName
    result.provenance["agent_name"] = ProvenanceEntry(key: "agent_name", value: cli.agentName, source: srcCli, detail: "--agent-name")

  if cli.secret.len > 0:
    result.secret = cli.secret
    result.provenance["secret"] = ProvenanceEntry(key: "secret", value: "[REDACTED]", source: srcCli, detail: "--secret")

  if cli.secretFile.len > 0:
    result.secretFile = expandPathSafe(cli.secretFile)
    result.provenance["secret_file"] = ProvenanceEntry(key: "secret_file", value: result.secretFile, source: srcCli, detail: "--secret-file")

  if cli.encrypt.isSome:
    let b = cli.encrypt.get()
    result.encrypt = b
    result.provenance["encrypt"] = ProvenanceEntry(key: "encrypt", value: $b, source: srcCli, detail: "--encrypt")

  if cli.cluster.isSome:
    let b = cli.cluster.get()
    result.cluster = b
    result.provenance["cluster"] = ProvenanceEntry(key: "cluster", value: $b, source: srcCli, detail: "--cluster")

  if cli.timeout.isSome:
    let t = cli.timeout.get()
    result.listenTimeout = t
    result.provenance["listen_timeout"] = ProvenanceEntry(key: "listen_timeout", value: $t, source: srcCli, detail: "--timeout")

  # Redis Cluster Hash Tag Enforcement
  if result.cluster and not (result.prefix.contains('{') and result.prefix.contains('}')):
    let base = if result.prefix.endsWith(":"): result.prefix[0 .. ^2] else: result.prefix
    result.prefix = "{" & base & ":" & result.project & "}:"
    result.provenance["prefix"] = ProvenanceEntry(key: "prefix", value: result.prefix, source: srcDefault, detail: "Cluster hash tag auto-applied")

# Starter TOML template
const starterTomlContent* = """# Locutus Configuration File (.locutus.toml)
# Tiered inter-assistant communication bus over Redis

# Redis connection endpoint
redis_url = "redis://127.0.0.1:6379"

# Key namespace prefix
prefix = "locutus:"

# Project isolation group
# project = "my-project"

# Cryptographic payload encryption (AES-256-CBC)
encrypt = false

# Redis Cluster support (automatically applies {prefix:project} hash tags)
cluster = false

# Heartbeat TTL in seconds for agent registration
heartbeat_ttl = 150

# Maximum message lifetime in seconds (default 7 days)
message_ttl = 604800

# Default listen timeout in seconds
listen_timeout = 90

# Optional path to shared secret file (kept out of version control)
# secret_file = "~/.config/locutus/secret"

# [profiles.staging]
# redis_url = "rediss://staging.internal:6380"
# prefix = "stg:locutus:"
# encrypt = true

# [profiles.prod]
# redis_url = "rediss://cluster.internal:6379"
# cluster = true
# encrypt = true
"""

proc formatConfigTable*(cfg: LocutusConfig): string =
  var lines: seq[string] = @[]
  lines.add("SETTING              VALUE                         SOURCE             DETAIL")
  lines.add("------------------------------------------------------------------------------------------------------------------------")
  
  let orderedKeys = [
    "redis_url", "prefix", "project", "agent_name", "secret", "secret_file",
    "encrypt", "cluster", "heartbeat_ttl", "message_ttl", "listen_timeout"
  ]
  for k in orderedKeys:
    if cfg.provenance.hasKey(k):
      let entry = cfg.provenance[k]
      let valStr = if entry.value.len > 28: entry.value[0..24] & "..." else: entry.value
      lines.add(entry.key.alignLeft(21) & valStr.alignLeft(30) & sourceLabel(entry.source).alignLeft(19) & entry.detail)
  
  if cfg.activeConfigFile.len > 0:
    lines.add("active_config".alignLeft(21) & cfg.activeConfigFile.alignLeft(30) & "file".alignLeft(19) & "loaded")
  lines.add("active_profile".alignLeft(21) & cfg.profile.alignLeft(30) & "profile".alignLeft(19) & "-")
  return lines.join("\n")

proc formatConfigJson*(cfg: LocutusConfig): string =
  var root = newJObject()
  for k, v in cfg.provenance:
    var entryObj = newJObject()
    entryObj["value"] = %v.value
    entryObj["source"] = %sourceLabel(v.source)
    entryObj["detail"] = %v.detail
    root[k] = entryObj
  root["active_profile"] = %cfg.profile
  if cfg.activeConfigFile.len > 0:
    root["active_config"] = %cfg.activeConfigFile
  return pretty(root)

proc formatConfigPaths*(): string =
  var lines: seq[string] = @[]
  let sys = getSystemConfigPath()
  let sysStatus = if fileExists(sys): "[FOUND]" else: "[NOT FOUND]"
  lines.add("System config    : " & sys & " " & sysStatus)

  let usr = getUserConfigPath()
  let usrStatus = if fileExists(usr): "[FOUND]" else: "[NOT FOUND]"
  lines.add("User config      : " & usr & " " & usrStatus)

  let ws = findWorkspaceConfigPath()
  let wsStatus = if ws.len > 0 and fileExists(ws): "[FOUND: " & ws & "]" else: "[NOT FOUND in current tree]"
  lines.add("Workspace config : " & wsStatus)
  return lines.join("\n")

proc initConfigFile*(target: string = "workspace"): string =
  let norm = target.toLowerAscii.strip(chars = {'-', ' '})
  let path = if norm in ["user", "global", "home", "u"]:
    getUserConfigPath()
  else:
    getCurrentDir() / ".locutus.toml"

  if fileExists(path):
    return "Error: Config file already exists at " & path
  
  createDir(path.splitPath.head)
  writeFile(path, starterTomlContent)
  return "Initialized Locutus configuration at: " & path
