# WorkPulse 项目网站（Qoder Sites 发布用）

这个目录是可直接发布的静态站点，不依赖构建工具、不引用任何外部 CDN，离线也能完整打开。

```
site/
├── index.html      # 单页站点（项目简介 / 痛点 / 功能 / 技术 / 效果 / 上手）
├── style.css       # 全部样式，无外部字体
└── images/
    ├── retrieval.png   # 混合检索界面（1600×1150）
    ├── tasks.png       # 任务看板（1600×1150）
    ├── report.png      # 周报生成（1600×1150）
    └── shot-*.png      # 六张完整视图截图，供文档与评审对照
```

## 发布步骤（Qoder Sites）

1. 在 Qoder 中打开 **Sites**，新建站点。
2. 站点根目录选择本目录 `qoder/workpulse/site/`（或把 `index.html`、`style.css`、`images/` 一起上传）。
3. 可见范围设置为 **任何人都可访问**（企业版账号无法设为公开，请用个人账号）。
4. 发布后，用无痕窗口打开站点地址，确认未登录也能访问、三张截图能正常显示。
5. 把站点地址填到提交平台，并写进仓库根 `README.md` 的「赛题信息」一节。

> 修改站点内容后需要再次点击「发布」才会更新线上版本。

## 本地预览

```bash
# 任选其一
python -m http.server 8080 -d site
# 或直接双击 index.html
```

## 内容口径

- 所有指标（检索 1–11ms、周报 0.6–1.0s、9 份资料 / 16 个分块 / 26 条待办）均来自本机实测，
  对应 `scripts/verify_sample.py` 与 `/api/evidence` 的输出，可复现。
- 截图来自本地运行的 WorkPulse（使用 `demo/` 下的虚构演示数据），不含任何真实用户信息。
