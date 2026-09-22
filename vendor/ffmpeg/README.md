# FFmpeg 二进制放置说明

本目录用于内置 FFmpeg，打包时会随 exe 一起发布，避免用户额外安装。

## 如何放置

1. 下载 Windows 版 FFmpeg（推荐官方 essentials 版本）
   - 地址: https://www.gyan.dev/ffmpeg/builds/
   - 直链: https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip

2. 解压后，只需要 `bin/` 目录里的两个文件：
   - `ffmpeg.exe`  (~80MB)
   - `ffprobe.exe` (可选，本项目暂不用)

3. 把它们放到当前目录 (`vendor/ffmpeg/`)：

   ```
   vendor/
     └── ffmpeg/
         ├── README.md          <- 本文件
         └── ffmpeg.exe         <- 你要放的
   ```

## 不放行不行？

可以。系统 PATH 中如果已经有 `ffmpeg`，程序会自动 fallback 到 PATH。
只是打包 exe 时用户机器上未必装了 ffmpeg，因此**发布前必须内置**。

## 版本要求

任意近 5 年的版本均可 (需要 `loudnorm` / `aresample` / `apad` 滤镜，2018 年后都支持)。
