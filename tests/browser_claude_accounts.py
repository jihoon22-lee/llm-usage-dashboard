"""Two Claude accounts under one Claude Code service, and a subscription marked ended.

A disposable synthetic store and Flask app serve every request: no provider, account
file, collector, private origin or production database is consulted.
"""
from browser_support import Sock, watch_csp, artifact_directory
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm_usage.store import Store  # noqa: E402
from llm_usage.webapp import create_app  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument('--artifacts', type=Path)
args = parser.parse_args()
output = artifact_directory(args.artifacts)
ORIGIN = 'https://dashboard.test'
SECOND = 'claude-code:abababababab'
now = time.time()
folder = tempfile.TemporaryDirectory(prefix='llm-claude-accounts-')
root = Path(folder.name)
store = Store(root/'usage.db')
with store.connect() as c:
    for route, plan, tier, label, homes in (('claude-code', 'pro', 'default_claude_ai', 'Pro', [dict(kind='default', name='~/.claude')]),
                                           (SECOND, 'max', 'default_claude_max_5x', 'Max 5x', [dict(kind='zerosub', name='claude-0123abcd4567')])):
        c.execute('INSERT INTO claude_accounts VALUES (?,?,?,?,?)', (route, 'k'*64 if route == 'claude-code' else 'f'*64, now-86400, now,
                  json.dumps(dict(primary=route == 'claude-code', plan=plan, tier=tier, plan_label=label, homes=homes,
                                  polled=homes[0], token_valid=True))))
    for route, five, seven, source in (('claude-code', 0, 25, 'claude-oauth'), (SECOND, 92, 97, 'claude-oauth:abababababab')):
        store.limit(c, route, 'five_hour', five, now+900, now-60, source)
        store.limit(c, route, 'seven_day', seven, now+3*86400, now-60, source)
        # The second account's poll is overdue: a delay chip that names the account.
        store.source(c, source, 'ok', '계정 사용량 조회 · 내부 OAuth 경로', now-60 if route == 'claude-code' else now-1200)
    store.limit(c, 'codex', 'codex · 10080분', 98, now+4*86400, now-60, 'codex')
    store.source(c, 'codex', 'ok', '', now-60)
    for bucket in ('daily', 'weekly'):
        store.limit(c, 'devin', bucket, 100, now+86400, now-60, 'devin')
    store.source(c, 'devin', 'ok', 'Free · 계정 사용량 조회', now-60)
    store.source(c, 'claude-accounts', 'ok', 'Claude 계정 2개 · 훅 기록 12건 · 계정 미확인 요청 3건', now-60)
    # A source that never succeeded is a chip at once; one that failed five minutes after a
    # success is not yet (the status tab shows both).
    store.source(c, 'claude-credits:abababababab', 'error', 'HTTP 500', now-120)
    store.source(c, 'claude-resets:abababababab', 'ok', '', now-400)
    store.source(c, 'claude-resets:abababababab', 'error', 'HTTP 503', now-100)
    for day in range(5):
        for i, (route, account, model, tokens) in enumerate((('claude-code', 'claude-code', 'claude-test', 4000), ('claude-code', SECOND, 'claude-test', 9000),
                                                            ('claude-code', 'claude-code:unknown', 'claude-test', 500), ('codex', None, 'gpt-test', 7000))):
            store.event(c, f'e-{day}-{i}', now-1800-day*86400, 'Anthropic' if route == 'claude-code' else 'OpenAI', route, model,
                        dict(uncached_input=tokens, cached_input=tokens*4, output=tokens//5), session=f'session-{i%2}',
                        project='fixture-project', agent_kind='main', account=account)
    store.save_state(c, 'collector', dict(status='idle', checked=now-30))
    store.save_state(c, 'last_local', now-30)
(root/'config.json').write_text('{}')
config = dict(database=str(root/'usage.db'), origin=ORIGIN, secret_key='fixture-key-'*4, allowed_logins=['fixture'],
              config_file=str(root/'config.json'), subscription_prices={'claude-code': 20, 'codex': 200},
              ended_subscriptions={'devin': {'since': now-3600}},
              pricing={'claude-test': dict(input=3, cached=.3, output=15), 'gpt-test': dict(input=2, cached=.2, output=8)})
client = create_app(config).test_client()


def serve(route):
    url = urlsplit(route.request.url)
    assert url.netloc == 'dashboard.test', route.request.url
    headers = {k: v for k, v in route.request.headers.items() if k.lower() in ('content-type', 'x-csrf-token', 'origin')}
    response = client.open(url.path+('?'+url.query if url.query else ''), method=route.request.method, data=route.request.post_data,
                           headers={**headers, 'Tailscale-User-Login': 'fixture', 'Host': 'dashboard.test'},
                           environ_base={'gunicorn.socket': Sock()})
    try:
        assert response.status_code < 400, (route.request.url, response.status_code)
        route.fulfill(status=response.status_code, body=response.get_data(),
                      headers={k: v for k, v in response.headers.items() if k.lower() != 'content-length'})
    finally:
        response.close()


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(service_workers='block', viewport={'width': 1440, 'height': 1000})
    page.route('**/*', serve)
    errors = []; page.on('pageerror', lambda e: errors.append(str(e))); watch_csp(page, errors)
    page.goto(ORIGIN+'/')
    expect(page.locator('#updated')).to_contain_text('마지막 갱신')
    # One Claude Code card, one section per account; no second Anthropic service.
    cards = page.locator('#limits > .limit-card')
    expect(cards.filter(has_text='Anthropic / Claude Code')).to_have_count(1)
    claude = page.locator('#quota-claude-code')
    expect(claude.locator('.quota-account')).to_have_count(2)
    expect(claude.locator('.quota-account-title').nth(0)).to_contain_text('Pro')
    expect(claude.locator('.quota-account-title').nth(0)).to_contain_text('기본 Claude 홈')
    expect(claude.locator('.quota-account-title').nth(1)).to_contain_text('Max 5x')
    expect(claude.locator('.quota-account-title').nth(1)).to_contain_text('zerosub 관리 홈 claude-0123abcd4567')
    expect(claude.locator('.quota-account').nth(1).locator('.remaining').first).to_contain_text('92%')
    expect(page.locator('#reset-timeline')).to_contain_text('Claude(Pro) 5시간')
    expect(page.locator('#reset-timeline')).to_contain_text('Claude(Max 5x) 5시간')
    expect(page.locator('#alerts')).to_contain_text('Claude(Pro) 5시간 잔여 0.0% · 다른 계정 Claude(Max 5x) 5시간 92.0%')
    expect(page.locator('#alerts')).to_contain_text('claude-credits · Max 5x 수집 실패')
    expect(page.locator('#alerts')).to_contain_text('claude-oauth · Max 5x 수집 지연')
    expect(page.locator('#alerts')).not_to_contain_text('claude-resets')
    # The ended Devin subscription folds with its reason and leaves the reset timeline.
    expect(page.locator('#reset-timeline')).not_to_contain_text('Devin')
    dormant = page.locator('.quota-dormant')
    expect(dormant).to_contain_text('Cognition / Devin')
    dormant.locator('summary').first.click()
    expect(dormant.locator('.quota-note').first).to_contain_text('구독 종료')
    page.screenshot(path=str(output/'claude-accounts-quota-1440.png'), full_page=True)
    # Usage by subscription account; the route view keeps one Claude Code service.
    page.locator('#tab-usage').click()
    expect(page.locator('#group option[value="account"]')).to_be_enabled()
    expect(page.locator('#legend')).to_contain_text('Anthropic / Claude Code')
    expect(page.locator('#legend')).not_to_contain_text('Max 5x')
    page.select_option('#group', 'account')
    expect(page.locator('#legend')).to_contain_text('Anthropic / Claude Code · Pro')
    expect(page.locator('#legend')).to_contain_text('Anthropic / Claude Code · Max 5x')
    expect(page.locator('#legend')).to_contain_text('Anthropic / Claude Code · 계정 미확인')
    expect(page.locator('#scope optgroup[label="Claude 계정"] option')).to_have_count(3)
    page.select_option('#scope', 'account:'+SECOND)
    expect(page.locator('#scope-chip')).to_contain_text('Claude 계정: Max 5x')
    expect(page.locator('#cards')).to_contain_text('45K')
    page.screenshot(path=str(output/'claude-accounts-usage-1440.png'), full_page=True)
    page.locator('#scope-chip').click()
    # Subscription value per Claude account; a missing price is visible, not 0.
    page.locator('#tab-reports').click()
    subvalue = page.locator('#subvalue')
    expect(subvalue).to_contain_text('Anthropic / Claude Code · Pro')
    expect(subvalue.locator('.sub-row', has_text='Max 5x')).to_contain_text('월 구독료 미설정')
    expect(subvalue).not_to_contain_text('Cognition / Devin')
    # Status sources name their account.
    page.locator('#tab-status').click()
    expect(page.locator('#sources')).to_contain_text('claude-oauth · Max 5x')
    expect(page.locator('#sources')).to_contain_text('claude-resets · Max 5x')
    expect(page.locator('#sources')).to_contain_text('claude-accounts')
    # Settings: a row per Claude account with a display name; the ended toggle is kept.
    page.locator('#settings-open').click()
    rows = page.locator('#cfg-subs .cfg-sub')
    expect(rows.filter(has_text='Anthropic / Claude Code · Max 5x')).to_have_count(1)
    expect(page.locator('#cfg-subs input[data-ended="devin"]')).to_be_checked()
    page.locator(f'#cfg-subs input[data-account-name="{SECOND}"]').fill('업무 Max')
    page.locator(f'#cfg-subs input[data-route="{SECOND}"]').fill('100')
    page.locator('#cfg-subs-save').click()
    expect(page.locator('#cfg-subs-msg')).to_contain_text('저장했습니다')
    saved = json.loads((root/'local.json').read_text())
    assert saved['claude_account_names'] == {SECOND: '업무 Max'}, saved
    assert saved['subscription_prices'][SECOND] == 100, saved
    assert list(saved['ended_subscriptions']) == ['devin'], saved
    page.screenshot(path=str(output/'claude-accounts-settings-1440.png'), full_page=True)
    page.locator('#settings-back').click()
    page.locator('#tab-quota').click()
    expect(page.locator('#quota-claude-code .quota-account-title').nth(1)).to_contain_text('업무 Max')
    expect(page.locator('#quota-claude-code .quota-account-title').nth(1)).to_contain_text('Max 5x')
    # Mobile: the overview lists both accounts in the one Claude row; nothing overflows.
    page.set_viewport_size({'width': 390, 'height': 844})
    page.reload()
    row = page.locator('#quota-overview [data-quota="claude-code"]')
    expect(row.locator('.ov-account')).to_have_count(2)
    expect(row).to_contain_text('업무 Max')
    expect(page.locator('#quota-overview [data-quota="devin"]')).to_have_count(0)
    row.click()
    expect(page.locator('#quota-claude-code')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(output/'claude-accounts-quota-390.png'), full_page=True)
    page.locator('#tab-usage').click()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.locator('#settings-open').click()
    expect(page.locator(f'#cfg-subs input[data-account-name="{SECOND}"]')).to_have_value('업무 Max')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(output/'claude-accounts-settings-390.png'), full_page=True)
    assert not errors, errors
    # Refreshes while a manual resource is being edited must keep its account.
    # Install the clock before navigation so the real 30-second aging timer runs.
    for width in (1440, 390, 320):
        context = browser.new_context(service_workers='block', viewport={'width': width, 'height': 900})
        editor = context.new_page()
        editor.clock.install()
        editor.route('**/*', serve)
        editor.goto(ORIGIN+'/')
        expect(editor.locator('#updated')).to_contain_text('마지막 갱신')
        editor.evaluate("$('auto').checked=false;schedule()")
        editor.locator('#resource-add').click()
        editor.locator('#resource-route').select_option(SECOND)
        label = f'계정 선택 보존 {width}'
        editor.locator('#resource-label').fill(label)
        editor.locator('#resource-amount').fill('7')
        editor.clock.fast_forward(31000)
        expect(editor.locator('#resource-route')).to_have_value(SECOND)
        editor.evaluate('()=>loadLimits()')
        editor.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        expect(editor.locator('#resource-route')).to_have_value(SECOND)
        expect(editor.locator('#resource-label')).to_have_value(label)
        expect(editor.locator('#resource-amount')).to_have_value('7')
        editor.locator('#resource-save').click()
        expect(editor.locator('#resource-message')).to_contain_text('기록을 저장했습니다.')
        expect(editor.locator('#resource-save')).to_be_enabled()
        with store.connect() as c:
            saved = c.execute("SELECT id,route FROM resource_items WHERE json_extract(data,'$.label')=?", (label,)).fetchone()
        assert saved['route'] == SECOND, dict(saved)
        editor.locator('#resource-cancel').click()
        editor.locator(f'[data-resource-edit="{saved["id"]}"]').click()
        editor.locator('#resource-amount').fill('9')
        editor.clock.fast_forward(31000)
        expect(editor.locator('#resource-route')).to_have_value(SECOND)
        editor.locator('#resource-save').click()
        expect(editor.locator('#resource-message')).to_contain_text('기록을 저장했습니다.')
        expect(editor.locator('#resource-save')).to_be_enabled()
        with store.connect() as c:
            changed = c.execute("SELECT route,json_extract(data,'$.amount') FROM resource_items WHERE id=?", (saved['id'],)).fetchone()
        assert tuple(changed) == (SECOND, 9), tuple(changed)
        # A disappearing account leaves an explicit invalid selection, never Codex.
        editor.evaluate("setClaudeAccounts(claudeAccounts.list.filter(a=>a.route==='claude-code'));syncResourceRoutes()")
        expect(editor.locator('#resource-route')).to_have_value(SECOND)
        assert not editor.locator('#resource-route').evaluate('(s)=>s.checkValidity()')
        editor.locator('#resource-route').select_option('codex')
        assert editor.locator('#resource-route').evaluate('(s)=>s.checkValidity()')
        # The same account is selectable when the default home is unidentified.
        editor.evaluate("setClaudeAccounts([{route:'claude-code:abababababab',label:'Max'}]);syncResourceRoutes()")
        editor.locator('#resource-route').select_option(SECOND)
        assert editor.locator('#resource-route').evaluate('(s)=>s.checkValidity()')
        context.close()
    browser.close()
folder.cleanup()
print('Claude accounts browser passed: quota, usage, value, settings, ended subscriptions and resource account selection across refreshes at three widths.')
