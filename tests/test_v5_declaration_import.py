"""V2 workbook parsing must not confer execution or publication authority."""
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import load_workbook

from app.governance.v2_import import DeclarationError, parse_v2_declarations


TEMPLATE = Path(__file__).resolve().parent / 'fixtures' / 'model_management_v2.xlsx'


def _sample():
    return load_workbook(TEMPLATE)


def _raw(wb):
    stream=BytesIO()
    wb.save(stream)
    return stream.getvalue()


def test_original_template_imports_as_unverified_draft():
    obj=parse_v2_declarations(TEMPLATE)
    assert obj['status']=='unverified_draft'
    assert len(obj['models'])==2
    assert len(obj['routes'])==4
    assert len(obj['caches'])==4


def test_duplicate_route_priority_is_rejected():
    wb=_sample()
    wb['渠道优先级']['E7']=1
    with pytest.raises(DeclarationError,match='duplicate'):
        parse_v2_declarations(_raw(wb))


def test_implicit_cache_resource_ttl_is_rejected():
    wb=_sample()
    wb['缓存设置']['G8']=60
    with pytest.raises(DeclarationError,match='TTL'):
        parse_v2_declarations(_raw(wb))


def test_expanded_rows_are_validated():
    wb=_sample()
    sheet=wb['渠道优先级']
    for col in 'ABCDEFG':
        sheet[f'{col}26']=sheet[f'{col}6'].value
    with pytest.raises(DeclarationError,match='duplicate'):
        parse_v2_declarations(_raw(wb))


def test_formula_in_declaration_is_rejected():
    wb=_sample()
    wb['渠道优先级']['C6']='=NOW()'
    with pytest.raises(DeclarationError,match='formulas are forbidden'):
        parse_v2_declarations(_raw(wb))


def test_invalid_forced_cache_contract_is_rejected():
    wb=_sample()
    wb['缓存设置']['D6']='强制'
    with pytest.raises(DeclarationError,match='forced'):
        parse_v2_declarations(_raw(wb))
