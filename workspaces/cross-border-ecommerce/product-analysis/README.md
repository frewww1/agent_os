# 选品分析模块 (Product Analysis)

跨境电商 AI 选品和市场数据分析工具集。

## 模块

- `keyword_trend.py` — 关键词趋势分析（热搜词、趋势变化）
- `competitor_analysis.py` — 竞品价格/销量监控
- `profit_calculator.py` — 利润计算器
- `scoring_model.py` — 选品评分模型
- `data_sources/` — 数据源适配层
- `utils.py` — 公共工具函数

## 安装

```bash
pip install -r requirements.txt
```

## 使用示例

```python
from keyword_trend import KeywordAnalyzer
from scoring_model import ProductScorer

# 分析关键词趋势
analyzer = KeywordAnalyzer()
trends = analyzer.analyze("手机壳", days=30)

# 评分选品
scorer = ProductScorer()
score = scorer.evaluate(product_data)
```
