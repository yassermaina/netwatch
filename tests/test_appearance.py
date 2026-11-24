import pytest

from netwatch.ui.appearance import APPEARANCES, LIGHT, USER_PRIORITY_PLUS_ONE, override_css


def test_follow_system_loads_nothing_so_shell_colours_apply():
    assert override_css("system") == ""


@pytest.mark.parametrize("appearance", ("light", "dark"))
def test_explicit_choice_sets_named_colours_and_root_variables(appearance):
    css = override_css(appearance)
    for name in ("window_bg_color", "window_fg_color", "view_bg_color", "headerbar_bg_color", "card_bg_color",
                 "sidebar_bg_color", "accent_bg_color"):
        assert f"@define-color {name} " in css
        assert f"--{name.replace('_', '-')}: " in css  # libadwaita 1.6+ reads :root properties


def test_dark_uses_netwatch_navy_palette_light_uses_libadwaita_defaults():
    assert "@define-color window_bg_color #1f2231;" in override_css("dark")
    assert f"@define-color window_bg_color {LIGHT['window_bg_color']};" in override_css("light")
    assert "nw_green" not in override_css("dark")  # app-specific colours stay in style-dark.css


def test_unknown_choice_rejected_and_priority_above_user():
    with pytest.raises(ValueError):
        override_css("sepia")
    assert APPEARANCES == ("system", "light", "dark") and USER_PRIORITY_PLUS_ONE == 801
