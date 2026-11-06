"""TTS 质量管线：文本规范化、参考音频预处理、音色相似度。

三个子模块都可在纯 CPU 环境独立导入；`tts_pipeline` 本身不导入 numpy，
因此只用文本规范化时不需要音频依赖。
"""

__all__ = ["ref_prep", "similarity", "text_norm"]
