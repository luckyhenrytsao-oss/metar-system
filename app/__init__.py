"""METAR 高频采集与分发系统.

提供从美国气象局 (weather.gov / SynopticData) 和 AviationWeather (AWC)
高频采集 METAR 报文，并通过 FastAPI + Redis 进行极速分发的能力。
"""

__version__ = "0.1.0"
