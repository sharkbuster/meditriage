"""症状分诊。

当检索做：主诉是 query，科室是 document，排序取 top-k。
默认模式不依赖任何模型，字符 n-gram + IDF 加权重叠，clone 下来就能跑。
换成本地模型或 API 只影响"建议"那段话术，不影响排序结果 ——
分诊排错了是医疗风险，不该交给概率模型。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from app.core.config import get_settings
from app.knowledge.departments import DEPARTMENTS, SYNONYMS
from app.services.llm import build_advice

# 中英文字符/数字切分，标点直接丢弃
_SPLIT = re.compile(r"[\s,，。、；;：:！!？?（）()\[\]【】\"'“”‘’/\\|+-]+")
_NGRAM_N = 2
# 覆盖率低于它就提示去导诊台。
# 用 evaluate.py 在 data/triage_eval.csv 上扫出来的：0.25 那档自动接诊
# 72/96 且全对，拦下 7/8 模糊主诉。调到 0.35 只多拦 1 条模糊的，
# 却要多推 24 条明确主诉给人工，不划算。改之前先重跑评测。
CONFIDENCE_THRESHOLD = 0.25


def normalize(text: str) -> str:
    """口语归一化。长词先替，否则"低烧"会先被"烧"吃掉。"""
    text = text.strip()
    for spoken in sorted(SYNONYMS, key=len, reverse=True):
        if spoken in text:
            text = text.replace(spoken, SYNONYMS[spoken])
    return text


def _tokens(text: str) -> list[str]:
    """切 unigram + bigram，用来打分。

    这里不过滤停用词：常见字的权重靠 IDF 自然压下去，
    而 bigram 正好提供"咳嗽""发热"这种关键词证据。覆盖率另走 _content_bigrams。
    """
    cleaned = "".join(_SPLIT.split(text))
    grams = list(cleaned)
    grams += [cleaned[i:i + _NGRAM_N] for i in range(len(cleaned) - _NGRAM_N + 1)]
    return grams


def _content_bigrams(text: str) -> list[str]:
    """提取有实义的内容单元，只给覆盖率用。

    不能拿 _tokens 算覆盖率：字符 bigram 会把停用词和术语粘成"了还""还喘"
    这类碎片，语料里根本没有，纯属稀释分母。实测"心慌胸闷"这么明确的主诉
    覆盖率只有 0.28，看着像说不清。所以先切句、去掉停用字，再取 bigram。

    去掉停用字后可能只剩单字（"肝功能异常" -> 肝/功/能/异/常），
    一个 bigram 都凑不出来，分母变 0 就成"没法判断"了，
    所以长度不足 2 的片段按单字留着。
    """
    units: list[str] = []
    for chunk in _SPLIT.split(text):
        kept = "".join(ch for ch in chunk if ch not in STOPWORDS)
        if len(kept) < _NGRAM_N:
            if kept:
                units.append(kept)
            continue
        units += [kept[i:i + _NGRAM_N] for i in range(len(kept) - _NGRAM_N + 1)]
    return units


# 停用字：只放"出现在术语里也不会改变科室判断"的字。
# 注意不要放"心、血、痛、吐"这类会构成医学术语的字。
STOPWORDS = set("的了着还也都很就才又再老是有点些许比较非常一直今天最近然后而且但是因为所以"
                "我你他她它我们你们他们请问一下一个什么怎么已经好像感觉觉得大约左右前后"
                "来去在于是和与跟对把被让给从到")


@dataclass(frozen=True)
class Match:
    department_id: int
    department_name: str
    score: float
    reason: str


class DepartmentIndex:
    """科室检索索引：IDF 权重 + 加权重叠打分。

    不碰数据库。传入什么科室列表就用什么，列表下标 +1 当 department_id，
    和 seed 写入的顺序对齐。
    """

    def __init__(self, departments: list[dict]) -> None:
        self.departments = departments
        self._for_child = [bool(d.get("for_child")) for d in departments]
        self._doc_tokens: list[Counter[str]] = []
        self._doc_len: list[int] = []
        df: Counter[str] = Counter()

        for dep in departments:
            doc_text = normalize(f"{dep['name']} {dep['keywords']} {dep['description']}")
            # 关键词再拼一遍，让主诉词比描述里的词权重高
            doc_text = f"{doc_text} {normalize(dep['keywords'])}"
            tokens = Counter(_tokens(doc_text))
            self._doc_tokens.append(tokens)
            self._doc_len.append(sum(tokens.values()))
            df.update(tokens.keys())

        total_docs = len(departments)
        # 平滑一下，避免除零；只在一个科室出现的词因此拿到高权重
        self._idf = {t: math.log((total_docs + 1) / (c + 0.5)) + 1.0 for t, c in df.items()}

    def coverage(self, symptom: str, department_id: int) -> float:
        """主诉里的实义词有多大比例能被这个科室解释。

        不拿打分当置信度的原因：打分按 query 总权重归一化过，
        主诉越长分数越小，长短之间没法比，定不了阈值。
        覆盖率是比例，含义也直白。
        """
        grams = _content_bigrams(normalize(symptom))
        if not grams:
            return 0.0
        idx = department_id - 1
        if not (0 <= idx < len(self._doc_tokens)):
            return 0.0
        doc = self._doc_tokens[idx]
        hit = sum(1 for gram in grams if gram in doc)
        return round(hit / len(grams), 4)

    def _weight(self, token: str) -> float:
        return self._idf.get(token, math.log(len(self.departments) + 1) + 1.0)

    def search(self, symptom: str, is_child: bool = False, top_k: int = 3) -> list[Match]:
        query = normalize(symptom)
        q_tokens = Counter(_tokens(query))
        if not q_tokens:
            return []

        results: list[Match] = []
        for i, dep in enumerate(self.departments):
            doc = self._doc_tokens[i]
            doc_len = self._doc_len[i] or 1
            overlap = 0.0
            hit_tokens: list[str] = []
            for token, q_count in q_tokens.items():
                d_count = doc.get(token, 0)
                if d_count == 0:
                    continue
                overlap += q_count * (d_count / doc_len) * self._weight(token)
                if len(token) == _NGRAM_N:          # 只把 bigram 命中当"关键词证据"
                    hit_tokens.append(token)

            if overlap <= 0:
                continue

            # 用 query 自身总权重归一化，使分数落在 0~1 且可跨 query 比较
            q_norm = sum(c * self._weight(t) for t, c in q_tokens.items()) or 1.0
            score = overlap / q_norm

            reason = self._build_reason(dep, hit_tokens)
            # department_id 用索引位置（1 起），与 seed 写入数据库的顺序一致
            results.append(Match(i + 1, dep["name"], round(min(score, 1.0), 4), reason))

        results.sort(key=lambda m: m.score, reverse=True)

        # 儿童患者先收敛到接诊儿童的科室。
        # 原来只是给儿科乘系数（1.15/1.20），评测证明没用，
        # "孩子拉肚子"照样首选消化内科，权重差太大扳不回来。
        if is_child:
            child_ok = [m for m in results if self._for_child[m.department_id - 1]]
            if child_ok:
                results = child_ok

        return results[:top_k]

    @staticmethod
    def _build_reason(dep: dict, hit_tokens: list[str]) -> str:
        """患者得能看懂为什么推荐这个科，不然不敢点。"""
        if not hit_tokens:
            return f"根据科室诊疗范围匹配到「{dep['name']}」"
        seen: list[str] = []
        for t in hit_tokens:
            if t not in seen:
                seen.append(t)
        sample = "、".join(seen[:4])
        return f"主诉中出现「{sample}」，属于{dep['name']}的常见就诊原因"


def confidence_of(matches: list[Match], index: DepartmentIndex, symptom: str) -> float:
    """首选科室对主诉的覆盖率。

    覆盖率低说明主诉太笼统（"不太舒服"），或者症状跨了好几个科，
    这种时候硬推荐一个不如让患者去导诊台。
    """
    if not matches:
        return 0.0
    return index.coverage(symptom, matches[0].department_id)


class TriageService:
    """对外入口，按配置决定要不要让模型润色建议。"""

    def __init__(self, departments: list[dict] | None = None) -> None:
        self.index = DepartmentIndex(departments or DEPARTMENTS)
        self.settings = get_settings()

    def triage(self, symptom: str, is_child: bool = False, top_k: int = 3) -> dict:
        matches = self.index.search(symptom, is_child=is_child, top_k=top_k)
        confidence = confidence_of(matches, self.index, symptom)
        need_human = confidence < CONFIDENCE_THRESHOLD or not matches

        advice = ""
        if need_human:
            advice = "症状描述指向不够明确，建议到一楼导诊台由护士协助分诊。"
        elif self.settings.triage_mode != "rule":
            # local / api 模式：模型只负责润色建议
            try:
                advice = self._generate_advice(symptom, matches)
            except Exception as exc:                      # noqa: BLE001
                print(f"[triage] 生成建议失败，回退规则话术: {type(exc).__name__}: {exc}")

        if not advice.strip():
            # 模型没报错但返回了空内容（代理拦截、模型抽风、响应结构变了都可能），
            # 这里必须兜住 —— 否则患者看到的是一个空的建议框。
            advice = build_advice([(m.department_id, m.department_name) for m in matches])

        return {
            "symptom": symptom,
            "candidates": [
                {
                    "department_id": m.department_id,
                    "department_name": m.department_name,
                    "score": m.score,
                    "reason": m.reason,
                }
                for m in matches
            ],
            "confidence": confidence,
            "mode": self.settings.triage_mode,
            "need_human": need_human,
            "advice": advice,
        }

    def _generate_advice(self, symptom: str, matches: list[Match]) -> str:
        """只在 local / api 模式下走到这里。"""
        from app.services.llm import generate_advice

        return generate_advice(symptom, [m.department_name for m in matches], self.settings)
