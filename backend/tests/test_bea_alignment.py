"""BEA is the default subject; source identity, expiry and old sessions cannot leak BOCOM rules."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.bank_faq import faq_configuration
from app.config import Settings
from app.main import create_app
from app.services_catalog import ServiceConfigUnavailable

ROOT = Path(__file__).resolve().parents[2]
BEA = ROOT / 'docs/知识库/东亚银行香港'
ARTICLES = json.loads((BEA / 'knowledge-base.json').read_text())['articles']

@pytest.fixture
def settings(tmp_path):
    return Settings(database_url=f'sqlite:///{tmp_path / "bea.sqlite3"}',
                    public_knowledge_path=BEA / 'public-base.json',
                    public_activation_path=BEA / 'public-activation.json',
                    bank_faq_path=BEA / 'knowledge-base.json',
                    bank_faq_activation_path=BEA / 'activation.json')

@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr('app.consultation.model_route', lambda *a, **kw: pytest.fail('No paid model call'))
    with TestClient(create_app(settings)) as c:
        yield c

def new(c, kind='personal'):
    r = c.post('/api/v1/sessions', json={'customer_type': kind})
    assert r.status_code == 201
    return r.json()['id']

def ask(c, sid, text):
    r = c.post(f'/api/v1/sessions/{sid}/messages', json={'text': text})
    assert r.status_code == 200, r.text
    return r.json()['answer']

def ids(a):
    return {i for s in a['sources'] for i in s['article_ids']}

@pytest.mark.parametrize('article', ARTICLES, ids=[a['id'] for a in ARTICLES])
@pytest.mark.parametrize('variant', [False, True])
def test_bea_questions_and_sources(client, article, variant):
    a = ask(client, new(client, 'enterprise' if article['customer_type'] == 'enterprise' else 'personal'),
            article['variants'][0] if variant else article['question'])
    assert article['id'] in ids(a), a['text']
    assert article['answer'] in a['text']
    assert all(r['url'].startswith('https://www.hkbea.com/') and r['jurisdiction'] == 'HK'
               and r['retrieved_on'] == '2026-10-11' for s in a['sources'] for r in s['references'])
    assert 'BCHK-' not in str(a) and '交银' not in a['text']

def test_context_and_catalog_default_bea(client):
    sid = new(client)
    assert 'HKFAQ-BEA-04' in ids(ask(client, sid, '换手机如何转移i-Token'))
    assert 'HKFAQ-BEA-04' in ids(ask(client, sid, '那怎么做'))
    catalog = client.get(f'/api/v1/sessions/{sid}/services').json()
    assert 'bocom' not in str(catalog) and 'bea' in str(catalog)

@pytest.mark.parametrize('q', ['交通银行香港普通定期起存多少', '东亚中国开户资格', '東亞銀行澳門網銀密碼重置', '香港分行开户', 'HSBC Singapore 转数快'])
def test_other_bank_or_region_cannot_use_bea(client, q):
    a = ask(client, new(client), q)
    assert not a['sources'] and a['kind'] != 'answered', a

def test_enterprise_facts_do_not_enter_personal_scope(client):
    a = ask(client, new(client), '电子结单保留多久')
    assert 'HKFAQ-BEA-19' not in ids(a)

def test_wrong_entity_source_fails_closed(settings, tmp_path):
    b = json.loads(settings.bank_faq_path.read_text())
    b['sources'][0]['url'] = 'https://hk.bankcomm.com/foo'
    path = tmp_path / 'wrong.json'
    path.write_text(json.dumps(b))
    with pytest.raises(ServiceConfigUnavailable):
        faq_configuration(path, settings.bank_faq_activation_path, [], [])

def test_expired_article_removed(client, settings, tmp_path):
    b = json.loads(settings.bank_faq_path.read_text())
    b['articles'][0]['review_due_on'] = '2026-10-12'
    path = tmp_path / 'expired.json'
    path.write_text(json.dumps(b))
    from unittest.mock import patch
    with patch('app.bank_faq.business_date', return_value='2026-10-12'):
        _, items = faq_configuration(path, settings.bank_faq_activation_path, [], [])
    assert 'HKFAQ-BEA-01' not in {f.id for i in items for f in i.public_facts}

def test_default_environment_uses_bea_without_changing_archives(monkeypatch):
    for key in ('PUBLIC_KNOWLEDGE_PATH', 'PUBLIC_ACTIVATION_PATH', 'BANK_FAQ_PATH', 'BANK_FAQ_ACTIVATION_PATH'):
        monkeypatch.delenv(key, raising=False)
    s = Settings.from_env()
    assert '东亚银行香港' in str(s.public_knowledge_path)
    assert '东亚银行香港' in str(s.bank_faq_path)
    assert (ROOT / 'docs/知识库/交通银行香港/knowledge-base.json').exists()

def test_reading_card_contains_all_relevant_official_sources(settings):
    from app.knowledge_base import knowledge_source
    _, items = faq_configuration(settings.bank_faq_path, settings.bank_faq_activation_path, [], [])
    personal = next(i for i in items if i.customer_type == 'personal')
    source = knowledge_source(personal)
    assert len(source.references) > 1
    assert any('hong-kong-dollars-deposits' in ref.url for ref in source.references)
    assert all(ref.publisher == '东亚银行有限公司（香港业务）' for ref in source.references)

@pytest.mark.parametrize('available', [True, False])
def test_v15_unnamed_current_question_uses_only_bea(settings, monkeypatch, available):
    from app.schemas import ConsultationContext
    from app.v15_answer import run_answer
    _, items = faq_configuration(settings.bank_faq_path, settings.bank_faq_activation_path, [], [])
    seen = []
    def fetch(url):
        seen.append(url)
        if not available:
            raise ValueError('Unavailable in isolated test')
        return '港元普通定期存款最低金额为港币1000元。具体条件请向东亚银行香港核对。'
    async def no_model(*args):
        raise ValueError('No paid model call during this regression')
    monkeypatch.setattr('app.v15_answer.fetch_page', fetch)
    monkeypatch.setattr('app.v15_answer.generate', no_model)
    result = run_answer('最新普通港元定存最低金额', 'personal', items, ConsultationContext(), settings, 'answer')
    assert seen and all(url.startswith('https://www.hkbea.com/') for url in seen)
    assert result.web_status == ('retrieved' if available else 'unavailable')
    assert '交银' not in result.answer.text
    if not available:
        assert not result.evidence
