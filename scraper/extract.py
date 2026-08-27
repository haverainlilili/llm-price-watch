"""Claude 结构化抽取: 官网价格页 -> PricingPage, 公告页 -> NewsPage。

实现要点(为什么这样写):
- 官方 anthropic SDK 的 client.messages.parse(output_format=模型):
  内部等价于 output_config={"format": {"type": "json_schema", ...}}, 响应
  经 Pydantic 校验, 校验不过 SDK 会自动带错误重试 —— 不需要自己写
  "请输出 JSON" 的解析循环。
- 模型默认 claude-opus-5; 可用环境变量 CLAUDE_MODEL 覆盖(如
  claude-sonnet-5, 抽取成本约为 opus-5 的 40%)。
- thinking 用自适应模式({"type": "adaptive"}): 解析混乱的 HTML 表格时
  自动启用推理。遇到 400(个别参数组合不被接受)会自动降级重试一次。
- system 和页面文本都加了 cache_control: SDK 对 429/5xx 的自动重试会
  命中前缀缓存, 重试几乎不额外花钱。
- 需要 ANTHROPIC_API_KEY 环境变量; 没有密钥时上层(run.py)直接跳过抽取。
"""
from __future__ import annotations

import os

import anthropic

from .models import NewsPage, PricingPage

MODEL = os.environ.get("CLAUDE_MODEL") or "claude-opus-5"
MAX_PAGE_CHARS = 250_000
MAX_TOKENS = 24000


class ExtractionError(RuntimeError):
    """一次抽取失败(网络/限流/校验), 调用方应保留旧数据并记录状态。"""


PRICING_SYSTEM = """你是一个严谨的大模型厂商官网价格页解析器, 把页面文本抽取成结构化数据。

规则:
1. 只抽取 API 按量计费(按 token)的价格, 单位统一为「每百万 tokens」。页面若按每千 tokens 计价, 换算成每百万。
2. currency 填该价格使用的币种: USD / CNY / EUR。
3. 输入价填 input_per_1m, 输出价填 output_per_1m, 缓存命中的输入价填 cached_input_per_1m。页面没写的字段留 null, 严禁编造或估算。
4. 限时折扣/活动价: 折后价填价格字段, 原价和活动说明写进 note, 例如「限时5折, 原价 ¥8/百万」。免费模型记 0 并在 note 注明「限时免费」。
5. 只关注 API 按量价格: 跳过订阅套餐(如 ChatGPT Plus / Claude Pro)、企业定制价、充值优惠。
6. 以对话/推理/多模态文本模型为主; embedding / rerank 等如果页面上有且价格简单, 也抽取并在 note 标注类型。
7. model 保留页面上的模型名原文。同一模型不同上下文档位价格不同时拆成多行, 在 note 标注档位。
8. promotions 汇总页面上明显的促销/活动文字(整段抄录), 没有则为 null。
9. 如果页面文本不含价格表(如 JS 渲染的空壳、报错页、人机验证页), 把 page_has_pricing 设为 false 且 models 留空。"""


NEWS_SYSTEM = """你从厂商官方公告 / changelog / 新闻页文本中抽取最近的公告条目。

规则:
- date: 页面标注的日期, 保留原文格式(如 2026-08-20 / Aug 20, 2026); 没有则 null
- title: 公告标题原文
- url: 文本中明确属于该条目的链接才填, 否则 null
- summary: 不超过 60 字的一句话中文摘要
- 只抽与该厂商模型/产品/价格相关的条目, 最多 12 条, 按页面出现顺序(新的在前)
- 页面没有公告(空壳/报错页)则 entries 留空"""


def has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _parse(client, system: str, user_text: str, output_format):
    def call(minimal: bool):
        kwargs = dict(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": [
                {"type": "text", "text": user_text,
                 "cache_control": {"type": "ephemeral"}},
            ]}],
            output_format=output_format,
        )
        if not minimal:
            kwargs["thinking"] = {"type": "adaptive"}
        return client.messages.parse(**kwargs)

    try:
        return call(minimal=False)
    except anthropic.BadRequestError as exc:
        # 个别参数组合(如 thinking + 结构化输出)在特定网关/版本下可能被拒,
        # 降级为最小参数集再试一次
        try:
            return call(minimal=True)
        except anthropic.BadRequestError as exc2:
            raise ExtractionError(f"请求被拒绝(400): {exc2.message}") from exc2
    except anthropic.RateLimitError as exc:
        raise ExtractionError("限流(429), SDK 自动重试后仍失败") from exc
    except anthropic.APIStatusError as exc:
        raise ExtractionError(f"API 错误({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ExtractionError(f"网络错误: {exc}") from exc
    except anthropic.AuthenticationError as exc:
        raise ExtractionError("认证失败: 检查 ANTHROPIC_API_KEY") from exc
    except ExtractionError:
        raise
    except Exception as exc:  # 校验失败等 —— 不能让单厂商异常拖垮整轮
        raise ExtractionError(f"{type(exc).__name__}: {exc}") from exc


def _page_text_header(provider: str, url: str, page_text: str) -> str:
    head = f"厂商: {provider}\nURL: {url}\n"
    if len(page_text) > MAX_PAGE_CHARS:
        head += f"(页面文本超过 {MAX_PAGE_CHARS} 字符, 已截断)\n"
    return f"{head}\n<page>\n{page_text[:MAX_PAGE_CHARS]}\n</page>\n"


def extract_pricing(provider: str, url: str, page_text: str) -> PricingPage:
    """抽取一个厂商价格页。失败抛 ExtractionError。"""
    client = anthropic.Anthropic()
    user_text = _page_text_header(provider, url, page_text) + "\n请抽取价格表。"
    resp = _parse(client, PRICING_SYSTEM, user_text, PricingPage)
    parsed = getattr(resp, "parsed_output", None)
    if parsed is None:
        raise ExtractionError("模型未返回可解析的结构化输出")
    # 清洗明显异常的行: 空名, 或输入输出都没价(通常是表头/误识别)
    parsed.models = [
        m for m in parsed.models
        if m and m.model and m.model.strip()
        and (m.input_per_1m is not None or m.output_per_1m is not None)
    ]
    return parsed


def extract_news(provider: str, url: str, page_text: str) -> NewsPage:
    """抽取一个厂商公告页。失败抛 ExtractionError。"""
    client = anthropic.Anthropic()
    user_text = _page_text_header(provider, url, page_text) + "\n请抽取公告条目。"
    resp = _parse(client, NEWS_SYSTEM, user_text, NewsPage)
    parsed = getattr(resp, "parsed_output", None)
    if parsed is None:
        raise ExtractionError("模型未返回可解析的结构化输出")
    return parsed
