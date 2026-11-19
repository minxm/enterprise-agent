"""Decide when a question is a contract lookup, and pull out the contract number.

Routing stays on keywords and the project contract-number shape. It does not
call a model.
"""

from __future__ import annotations

import re

_NUMBER = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,12}-\d{2,8}(?:-\d{2,8})?)(?![A-Za-z0-9])")
_LABELED = re.compile(r"合同编号\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9._-]{0,63})")
_CONTRACT = "合同"
_LOOKUP = re.compile(r"查询|查看|查一下|合同编号|基本信息|当前版本|审批状态")
_EXPLICIT = re.compile(r"查询|查看|查一下")
_FIELDS = re.compile(r"合同编号|基本信息|当前版本|审批状态")
MISSING_NUMBER = "请提供要查询的合同编号，例如：查询合同编号 HT-2026-001 的基本信息、当前版本和审批状态。"
AMBIGUOUS_NUMBER = "问题里有多个合同编号。请一次只查询一个编号。"


def looks_like_contract_query(question: object) -> bool:
    if not isinstance(question, str) or _CONTRACT not in question:
        return False
    if extract_contract_numbers(question) and _LOOKUP.search(question):
        return True
    return _EXPLICIT.search(question) is not None and _FIELDS.search(question) is not None


def extract_contract_numbers(question: object) -> list[str]:
    if not isinstance(question, str):
        return []
    found: list[str] = []
    for match in _LABELED.finditer(question):
        _add(found, match.group(1))
    for match in _NUMBER.finditer(question):
        _add(found, match.group(1))
    return found


def _add(found: list[str], value: str) -> None:
    number = value.strip()
    if number and number not in found:
        found.append(number)
