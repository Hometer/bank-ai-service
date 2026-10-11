"""Historical source regressions run on their recorded verification date; current BEA tests use today."""
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def historical_public_source_date(request, monkeypatch):
    if Path(str(request.node.fspath)).stem in {
        'test_bank_faq', 'test_public_knowledge', 'test_v15'
    }:
        monkeypatch.setattr('app.knowledge_base.business_date', lambda: '2026-10-03')
        monkeypatch.setattr('app.bank_faq.business_date', lambda: '2026-10-03')
