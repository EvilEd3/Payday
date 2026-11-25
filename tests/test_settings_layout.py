"""Settings has a side nav of sections, and the activity log lives inside it rather than in the top menu."""
from test_security import _signed_in, _user


def test_settings_nav_lists_sections_and_the_activity_log(client):
    _signed_in(client, _user(client, "navadmin"))
    html = client.get("/settings?tab=rules").text
    assert 'class="setnav"' in html and 'class="setshell"' in html  # a section, not the phone list
    assert '<a href="/settings?tab=rules" aria-current="page">Rules' in html
    assert '<a href="/activity" >Activity' in html or '<a href="/activity">Activity' in html
    assert 'class="tabs" aria-label="Settings sections"' not in html


def test_plain_settings_is_the_section_list_on_phones(client):
    _signed_in(client, _user(client, "navadmin"))
    assert 'class="setshell index"' in client.get("/settings").text


def test_activity_is_out_of_the_top_menu_and_opens_inside_settings(client):
    _signed_in(client, _user(client, "navadmin"))
    html = client.get("/activity").text
    nav = html.split('<nav class="nav"', 1)[1].split("</nav>", 1)[0]
    assert "/activity" not in nav
    assert '<a href="/settings" aria-current="page">Settings</a>' in nav
    assert '<a href="/activity" aria-current="page">Activity' in html
