# 课程自动整理（V2）安装说明

1. 将本目录作为本地插件仓库挂载到 MoviePilot。
2. 确保仓库路径已加入 `PLUGIN_LOCAL_REPO_PATHS`。
3. 推荐目录映射：
   - `incoming`：`/volume1/未整理`（下载/待整理来源）
   - `tv_output`：`/volume1/TV`（电视剧目标媒体库）
   - `movie_output`：`/volume1/Movies`（电影目标媒体库，页面显示“电影”）
   - `children_output`：`/volume1/儿童`（儿童目标媒体库）
4. 保证以上四个目录在容器内为正确挂载；插件只扫描 `incoming`。
5. 重启 MoviePilot 并在插件列表启用「课程自动整理」。

插件支持 `off` / `preview` / `apply` 三种命名模式。

升级建议流程（v1.5.3）：

1. 保持 `naming_mode=off` 完成一轮兼容验证。
2. 切换为 `naming_mode=preview`，观察详情页是否有合理的候选与建议。
3. 对需要干预的课程使用 `naming_manual_overrides`。
4. 全量确认后再切换 `naming_mode=apply`。

v1.5.3 说明：旧 `output` 会在缺少新字段时迁移为 `children_output`。电视剧、电影和儿童是三个独立目标；TMDB/豆瓣与 DeepSeek 分类低于 0.90、相互冲突或不可用时只预览、不移动。配置页增加小视口滚动与路径换行，高级设置默认折叠；运行日志可按 `CourseOrganizer[event=...]` 检索完整处理阶段；一次性运行请求在排队期间保持为 true，在回调取得执行权或显式停止时复位为 false，配置重载不会取消已排队回调。

安装不自动修改 NAS 上既有 TV、Movies、儿童目录，也不触发一次性扫描或大规模重命名。
