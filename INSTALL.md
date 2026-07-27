# 课程自动整理（V2）安装说明

1. 将本目录作为本地插件仓库挂载到 MoviePilot。
2. 确保仓库路径已加入 `PLUGIN_LOCAL_REPO_PATHS`。
3. 推荐目录映射：
   - `incoming`：`/volume1/未整理`（下载/待整理来源）
   - `output`：`/volume1/儿童`（课程整理输出）
   - `tv`：`/volume1/TV`（MoviePilot 原生影视目录，**插件不得扫描或改动**）
4. 保证 `incoming` 与 `output` 在容器内为可读写挂载。
5. 重启 MoviePilot 并在插件列表启用「课程自动整理」。

插件只负责文件整理，不进行网络请求与元数据抓取。
