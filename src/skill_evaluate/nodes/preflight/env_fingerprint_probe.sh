#!/bin/sh
# 沙箱环境指纹探测脚本（docs/dev/21 第 4 节）。
#
# 由 `HermesSandboxClient.run_environment_probe()` 下发到与评测任务**同一基础镜像**的沙箱中执行，
# stdout 输出一个 JSON 对象，字段与 `nodes/preflight/fingerprint.py::SandboxFingerprint` 一一对应。
#
# 设计约束：
# - 只用 POSIX sh + coreutils：基础镜像里未必有 python/jq，探测脚本本身不能成为新的环境依赖；
# - 只读、无网络、秒级完成：它跑在沙箱初始化 Hook 里，慢一秒就是每次评测慢一秒；
# - 工具不存在就**不输出**该键（而不是输出 "missing"）：键集合本身就是指纹的一部分，
#   黄金指纹里有 node、当前环境没有，比对时自然报"缺失"；
# - 环境变量只取白名单里的结构性变量，绝不输出任何密钥（服务端还会再按名字过滤一遍）。
#
# 随基础镜像一并维护：改了本脚本的输出口径 = 必须重新生成并人工确认 golden_fingerprint.json。

set -u

json_escape() {
  # 反斜杠与双引号转义；控制字符（换行/回车/制表）替换为空格，保证单行 JSON 可解析。
  printf '%s' "$1" | tr '\n\r\t' '   ' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

sha256_of_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | cut -d' ' -f1
  else
    # 连哈希工具都没有时输出固定标记：黄金指纹里是真实哈希，比对必然不一致，门禁照样拦下。
    cat >/dev/null
    printf 'no-sha256-tool'
  fi
}

# 累积 "key":"value" 片段，逗号分隔。
append_pair() {
  # $1=当前累积串 $2=key $3=value
  if [ -z "$1" ]; then
    printf '"%s":"%s"' "$(json_escape "$2")" "$(json_escape "$3")"
  else
    printf '%s,"%s":"%s"' "$1" "$(json_escape "$2")" "$(json_escape "$3")"
  fi
}

# ---- 结构性环境变量白名单（必须在下面固定 LC_ALL 之前采集，否则快照的是脚本自己设的值） ----
envs=""
for name in PATH LANG LC_ALL TZ HOME SHELL USER PYTHONPATH NODE_PATH VIRTUAL_ENV; do
  eval "is_set=\${$name+x}"
  if [ -n "$is_set" ]; then
    eval "value=\${$name}"
    envs="$(append_pair "$envs" "$name" "$value")"
  fi
done

# 之后的命令输出（排序、版本串）固定用 C locale，保证同一镜像两次探测逐字节一致。
LC_ALL=C
export LC_ALL

# ---- 内核与 OS ----
os_kernel="$(uname -sr 2>/dev/null || printf 'unknown')"
if [ -r /etc/os-release ]; then
  os_id="$(. /etc/os-release 2>/dev/null; printf '%s %s' "${ID:-}" "${VERSION_ID:-}")"
  os_kernel="$os_kernel | $os_id"
fi

# ---- 运行时版本（只取第一行，去掉各工具版本输出里的无关尾巴） ----
runtimes=""
probe_version() {
  # $1=键名 $2...=命令
  key="$1"; shift
  if command -v "$1" >/dev/null 2>&1; then
    value="$("$@" 2>&1 | head -n 1)"
    runtimes="$(append_pair "$runtimes" "$key" "$value")"
  fi
}
probe_version python python3 --version
probe_version pip python3 -m pip --version
probe_version node node --version
probe_version npm npm --version
probe_version git git --version
probe_version bash bash --version
probe_version uv uv --version
probe_version java java -version

# ---- 关键依赖包清单哈希（排序后哈希，顺序无关） ----
packages=""
if command -v python3 >/dev/null 2>&1; then
  h="$(python3 -m pip list --format=freeze 2>/dev/null | sort | sha256_of_stdin)"
  packages="$(append_pair "$packages" "python_packages" "$h")"
fi
if command -v npm >/dev/null 2>&1; then
  h="$(npm ls -g --depth=0 --parseable 2>/dev/null | sort | sha256_of_stdin)"
  packages="$(append_pair "$packages" "npm_global_packages" "$h")"
fi
if command -v dpkg-query >/dev/null 2>&1; then
  h="$(dpkg-query -W -f='${Package}=${Version}\n' 2>/dev/null | sort | sha256_of_stdin)"
  packages="$(append_pair "$packages" "dpkg_packages" "$h")"
elif command -v apk >/dev/null 2>&1; then
  h="$(apk info -v 2>/dev/null | sort | sha256_of_stdin)"
  packages="$(append_pair "$packages" "apk_packages" "$h")"
fi

printf '{"os_kernel":"%s","runtime_versions":{%s},"key_env_vars_snapshot":{%s},"core_package_hashes":{%s}}\n' \
  "$(json_escape "$os_kernel")" "$runtimes" "$envs" "$packages"
