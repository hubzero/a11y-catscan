"""A minimal login plugin used by the auth lifecycle tests.

Mirrors the real plugin contract (login / is_logged_in / exclude_paths)
but does no real authentication — it just records that it ran and
returns whatever is configured in the fixture's config dict under
auth.fake_outcome ('success', 'fail', or 'raise').
"""

# These globals get reset by the test before each run.
calls = {'login': 0, 'is_logged_in': 0, 'init_from_context': 0}
exclude_paths = ['/logout-test']


def reset():
    calls['login'] = 0
    calls['is_logged_in'] = 0
    calls['init_from_context'] = 0


async def login(context, config):
    calls['login'] += 1
    outcome = (config.get('auth') or {}).get('fake_outcome', 'success')
    if outcome == 'raise':
        raise RuntimeError('simulated login failure')
    return outcome == 'success'


async def is_logged_in(page):
    calls['is_logged_in'] += 1
    return True


async def init_from_context(context):
    calls['init_from_context'] += 1
