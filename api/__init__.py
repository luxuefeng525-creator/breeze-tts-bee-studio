"""Bee Studio 后端扩展（视频 API 路由 + 成本估算 + ffmpeg 合成）。

模块结构：
  providers/    —— 四家视频 API 的统一封装（Jimeng / Kling / Vidu / Wan3.0）
  cost_calc.py  —— 实时成本估算（各模型定价表 + 公式）
  ffmpeg_export —— 本地 ffmpeg 管线（切片 D，占位）

注意：provider 的 ENDPOINT / PATH 不能瞎编，各家官网拿。占位用 YYYY_ENDPOINT。
"""
