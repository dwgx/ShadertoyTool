# ShadertoyTool

从 [Shadertoy](https://www.shadertoy.com) 下载 GLSL shader 源码及关联资源（纹理、音频、视频、Cubemap）的命令行工具。

## 功能

- 通过 URL 或 Shader ID 下载完整的 GLSL 源码
- 自动提取所有 Render Pass（Image / Buffer / Sound / Common 等）
- 下载 Shader 引用的纹理、音频、视频等资源文件
- 生成资源清单 `manifest.json`，记录下载状态
- 支持 Shadertoy API Key 直接访问
- 支持浏览器 Cookie 自动提取，绕过登录限制
- 内置 Playwright + Edge 浏览器回退，应对 Cloudflare 验证

## 安装

### 依赖

- Python 3.8+
- pip

```bash
pip install -r requirements.txt
```

Playwright 还需要安装浏览器驱动（仅在需要浏览器回退时）：

```bash
python -m playwright install chromium
```

### Windows 快捷方式

直接双击或在命令行运行 `fetch_shadertoy.cmd`，会自动检测并安装缺失依赖。

## 使用

### 基本用法

```bash
# 通过 URL 下载
python fetch_shadertoy.py https://www.shadertoy.com/view/4djyRD

# 通过 Shader ID 下载
python fetch_shadertoy.py 4djyRD

# Windows 批处理
fetch_shadertoy.cmd XltGDr
```

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `shader` | Shadertoy URL 或 6 位 Shader ID | `4djyRD` |
| `-o, --out-dir` | GLSL 输出目录 | `downloads` |
| `--api-key` | Shadertoy API Key | 无 |
| `--skip-assets` | 跳过资源下载 | 否 |
| `--assets-dir` | 资源保存目录 | `<out-dir>/<id>_assets` |
| `--no-browser` | 禁用浏览器回退 | 否 |
| `--headless` | 浏览器回退使用无头模式 | 否 |
| `--verify-attempts` | 手动验证重试次数 | `5` |

### 示例

```bash
# 使用 API Key 下载（推荐，最稳定）
python fetch_shadertoy.py XltGDr --api-key YOUR_API_KEY

# 只下载 GLSL 源码，不下载资源
python fetch_shadertoy.py 4d33Dj --skip-assets

# 指定输出目录
python fetch_shadertoy.py XtlSD7 -o ./my_shaders

# 无头模式浏览器回退
python fetch_shadertoy.py 4djyRD --headless
```

## 输出结构

```
downloads/
├── XltGDr.glsl              # GLSL 源码（含所有 Pass）
└── XltGDr_assets/
    ├── manifest.json         # 资源下载清单
    ├── texture_01.png        # 纹理
    └── audio_track.mp3       # 音频
```

### GLSL 文件格式

每个 `.glsl` 文件包含该 Shader 的所有 Render Pass，以注释分隔：

```glsl
// ShaderToy ID: XltGDr
// Name: Contra
// Retrieved: 2025-01-01 12:00:00 UTC

// ===== Pass 0: Image (image) =====
void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    // ...
}

// ===== Pass 1: Buffer_A (buffer) =====
void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    // ...
}
```

## 获取 API Key

1. 注册 [Shadertoy](https://www.shadertoy.com) 账号
2. 进入 [Apps 页面](https://www.shadertoy.com/myapps) 创建应用
3. 复制生成的 API Key

使用 API Key 可以直接通过官方 API 获取数据，无需处理 Cloudflare 验证，是最稳定的方式。

## 工作原理

工具按以下优先级尝试获取 Shader 数据：

1. **API v1**（需要 `--api-key`）— 官方 REST API，最可靠
2. **Legacy POST** — 模拟网页端的内部请求，附带浏览器 Cookie
3. **Playwright 浏览器回退** — 启动 Edge 浏览器，通过 Cloudflare 验证后在页面内发起请求

资源下载同样支持浏览器回退：先用 requests 直接下载，403 失败的资源会自动通过浏览器重试。

## License

[MIT](LICENSE)
