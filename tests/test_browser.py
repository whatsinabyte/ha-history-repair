"""Browser tests for the frontend, driven against a real running server.

These exist because the JavaScript is where the user actually works — reading
the graph, clicking a bad point, correcting it — and none of it is exercised
by the Python suite. A rendered Chart.js canvas cannot be asserted on
pixel-by-pixel, but everything around it can: that the page loads its data,
that clicking a point opens the panel with the right values, that saving
reaches the API and the graph updates, and that the warnings a user must not
miss are actually on screen.

The server runs in-process against FakeAdapter, so no database is needed and
these run in CI alongside everything else.

Skipped unless Playwright and its browser are installed:

    .venv-check/bin/python -m pip install pytest-playwright
    .venv-check/bin/playwright install chromium
"""

from __future__ import annotations

import itertools
import re
import threading
import time
from collections.abc import Iterator
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest

from hr_fakes import SEED_TIMESTAMPS, FakeAdapter
from hr_models import Quality, SensorType

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright
expect = playwright_api.expect


class _QuietHandler(WSGIRequestHandler):
    """Keep the request log out of the pytest output."""

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def live_server(app: Any) -> Iterator[str]:
    """Serve the Flask app on an ephemeral port for the duration of a test.

    Port 0 lets the OS choose a free port, so parallel workers cannot collide
    on a fixed one.
    """
    server = make_server("127.0.0.1", 0, app, handler_class=_QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def page(live_server: str) -> Iterator[Any]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        browser_page = context.new_page()
        errors: list[str] = []
        browser_page.on("pageerror", lambda exc: errors.append(str(exc)))
        browser_page.base_url = live_server  # type: ignore[attr-defined]
        yield browser_page
        context.close()
        browser.close()
        # A JavaScript exception must fail the test even when the assertions
        # happened to pass around it.
        assert not errors, f"JavaScript errors on the page: {errors}"


def _goto(page: Any, path: str) -> None:
    page.goto(f"{page.base_url}{path}", wait_until="networkidle")


ENTITY = "sensor.living_room_temperature"


_CONTRAST_RATIO_JS = """
(selector) => {
  const el = document.querySelector(selector);
  const style = getComputedStyle(el);
  // Pills and links don't paint their own background, so this walks up to
  // find the nearest ancestor that actually does.
  let bgEl = el;
  let bg = getComputedStyle(bgEl).backgroundColor;
  while (bgEl && bg === 'rgba(0, 0, 0, 0)') {
    bgEl = bgEl.parentElement;
    bg = bgEl ? getComputedStyle(bgEl).backgroundColor : 'rgb(255, 255, 255)';
  }
  function toRgb(c) {
    const m = c.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
    return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : [0, 0, 0];
  }
  function luminance([r, g, b]) {
    const [rs, gs, bs] = [r, g, b].map((c) => {
      const v = c / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * rs + 0.7152 * gs + 0.0722 * bs;
  }
  const l1 = luminance(toRgb(style.color));
  const l2 = luminance(toRgb(bg));
  const lighter = Math.max(l1, l2);
  const darker = Math.min(l1, l2);
  return (lighter + 0.05) / (darker + 0.05);
}
"""


class TestNoHorizontalOverflow:
    """No page should ever need a horizontal scrollbar, at any width.

    Every previous overflow report turned out to be a real bug in one
    specific element at one specific width — the table's column sizing, the
    heading's word-breaking, the header row's flex-wrap — found and fixed one
    at a time. This checks the general property directly, across a spread of
    real device widths and both the worst-case (very long names) and normal
    seeded data, so a *new* overflow-causing element gets caught here instead
    of by another manual report.
    """

    @pytest.mark.parametrize("width", [320, 375, 480, 600, 640, 700, 800, 1024])
    def test_entities_page_never_overflows(
        self, page: Any, adapter: FakeAdapter, width: int
    ) -> None:
        adapter.add_entity("sensor." + "x" * 80)
        adapter.add_entity("sensor.energy_total", SensorType.COUNTER)
        adapter.add_statistics_metadata("sensor.energy_total", mean_type=0, has_sum=True)
        page.set_viewport_size({"width": width, "height": 900})
        _goto(page, "/")
        body_width = page.evaluate("() => document.documentElement.scrollWidth")
        assert body_width <= width, f"entities page overflows at {width}px: {body_width}px"

    @pytest.mark.parametrize("width", [320, 375, 480, 600, 640, 700, 800, 1024])
    def test_audit_page_never_overflows(self, page: Any, adapter: FakeAdapter, width: int) -> None:
        long_id = "sensor." + "y" * 80
        adapter.add_entity(long_id)
        adapter.add_state(long_id, 99, 1_700_000_000.0, "1.0")
        adapter.apply_correction(
            entity_id=long_id,
            state_id=99,
            expected_original="1.0",
            new_value="2.0",
            quality=Quality.SPIKE,
            note="a" * 80,
            created_by="a fairly long username",
        )
        page.set_viewport_size({"width": width, "height": 900})
        _goto(page, "/audit")
        body_width = page.evaluate("() => document.documentElement.scrollWidth")
        assert body_width <= width, f"audit page overflows at {width}px: {body_width}px"

    @pytest.mark.parametrize("width", [320, 375, 480, 600, 640, 700, 800, 1024])
    def test_entity_graph_page_never_overflows(
        self, page: Any, adapter: FakeAdapter, width: int
    ) -> None:
        long_id = "sensor." + "z" * 80
        adapter.add_entity(long_id)
        page.set_viewport_size({"width": width, "height": 900})
        _goto(page, f"/entity/{long_id}")
        body_width = page.evaluate("() => document.documentElement.scrollWidth")
        assert body_width <= width, f"entity page overflows at {width}px: {body_width}px"

    @pytest.mark.parametrize("width", [320, 375, 480, 600, 640, 700, 800, 1024])
    def test_onboarding_page_never_overflows(self, fresh_app: Any, width: int) -> None:
        # fresh_app (not yet onboarded) needs its own server, the same way
        # TestOnboarding below does — the shared `page`/`live_server`
        # fixtures are tied to the already-onboarded `app` fixture instead.
        server = make_server("127.0.0.1", 0, fresh_app, handler_class=_QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()
                page.set_viewport_size({"width": width, "height": 900})
                page.goto(base, wait_until="networkidle")
                body_width = page.evaluate("() => document.documentElement.scrollWidth")
                assert body_width <= width, (
                    f"onboarding page overflows at {width}px: {body_width}px"
                )
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _banner_content_box(page: Any) -> dict[str, float]:
    return page.evaluate("""() => {
        const banner = document.querySelector('.banner');
        const cs = getComputedStyle(banner);
        const rect = banner.getBoundingClientRect();
        return {
            left: rect.left + parseFloat(cs.paddingLeft) + parseFloat(cs.borderLeftWidth),
            right: rect.right - parseFloat(cs.paddingRight) - parseFloat(cs.borderRightWidth),
        };
    }""")


class TestTableWidth:
    """The entities and corrections tables should be sized and positioned
    to exactly match .banner's own content box, not derived independently.

    Two earlier attempts each shrank the table by a number reasoned out on
    its own (a calc() on the table, then extra container padding, then a
    further shrink after a rounded-corner clash) — every one of them passed
    its own test yet still didn't look right, because each invented its own
    number instead of reusing one already agreed to be correct: .banner's.
    This checks the table's left and right edges land exactly where
    .banner's text does, since both containers (.card and .banner) already
    sit at the same left/right position in the page.
    """

    def test_the_entity_table_matches_the_banner_exactly(self, page: Any) -> None:
        page.set_viewport_size({"width": 900, "height": 900})
        _goto(page, "/")
        banner_box = _banner_content_box(page)
        table_box = page.evaluate("""() => {
            const rect = document.querySelector('.entity-table').getBoundingClientRect();
            return {left: rect.left, right: rect.right};
        }""")
        assert abs(table_box["left"] - banner_box["left"]) < 1
        assert abs(table_box["right"] - banner_box["right"]) < 1

    def test_the_audit_table_matches_the_banner_exactly(self, page: Any) -> None:
        page.set_viewport_size({"width": 900, "height": 900})
        _goto(page, "/audit")
        banner_box = _banner_content_box(page)
        table_box = page.evaluate("""() => {
            const rect = document.querySelector('.audit-table').getBoundingClientRect();
            return {left: rect.left, right: rect.right};
        }""")
        assert abs(table_box["left"] - banner_box["left"]) < 1
        assert abs(table_box["right"] - banner_box["right"]) < 1


class TestMobileStackedRowsFitTheScreen:
    """Below the 640px breakpoint, each table row becomes its own block-level
    "card" (.stack-table tr), inset from the screen edge by its own 12px
    side margin.

    That margin combined with an explicit `width: 100%` on the same rule
    computed to 24px *wider* than the table itself — width and margin are
    independent in CSS, so `width: 100%` does not leave room for margin the
    way `width: auto` does. On a narrow phone this pushed each row a few
    pixels past the right edge of the actual viewport. Found on a real
    iPhone via Safari's dev tools — this project's own browser tests all run
    at desktop-ish sizes and never exercised a viewport this narrow with
    `TestNoHorizontalOverflow`'s style of check on the *rows themselves*
    (that suite checks `document.documentElement.scrollWidth`, which missed
    this because the ~3px row overflow never actually forced the whole page
    to grow — WebKit and Chromium both just let the row visually spill past
    its own container without enlarging the document).
    """

    @pytest.mark.parametrize("width", [320, 375, 390, 414, 480, 600])
    def test_no_row_extends_past_the_viewport(
        self, page: Any, adapter: FakeAdapter, width: int
    ) -> None:
        adapter.add_entity("sensor." + "x" * 80)
        page.set_viewport_size({"width": width, "height": 900})
        _goto(page, "/")
        max_row_right = page.evaluate("""() => {
            const rows = Array.from(document.querySelectorAll('.entity-table tbody tr'));
            return Math.max(...rows.map((tr) => tr.getBoundingClientRect().right));
        }""")
        assert max_row_right <= width, f"a row extends to {max_row_right}px at {width}px wide"


class TestColorContrast:
    """WCAG-style contrast checks, in both the light and dark palettes.

    Not a full accessibility audit — just the specific pattern that was
    reported unreadable: a semantic colour (accent/danger/warn/ok) used
    directly as text or a pill's colour, against whatever surface it sits on.
    Home Assistant add-ons cannot ask the viewer which theme they use —
    Ingress has no such setting — so both prefers-color-scheme palettes have
    to hold up on their own.
    """

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_pill_colours_are_readable_against_their_background(
        self, page: Any, adapter: FakeAdapter, scheme: str
    ) -> None:
        adapter.add_entity("sensor.energy_total", SensorType.COUNTER)
        adapter.add_statistics_metadata("sensor.energy_total", mean_type=0, has_sum=True)
        adapter.apply_correction(
            entity_id=ENTITY,
            state_id=2,
            expected_original="-2000.0",
            new_value="20.2",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        page.emulate_media(color_scheme=scheme)
        _goto(page, "/")

        # 3:1 is the WCAG AA threshold for large/bold text and UI components
        # (not the stricter 4.5:1 for small body text) — pills use a bold,
        # small-but-not-tiny label, which is the category that threshold
        # actually covers.
        for selector in (".pill.measurement", ".pill.counter", ".pill.corrected"):
            ratio = page.evaluate(_CONTRAST_RATIO_JS, selector)
            assert ratio >= 3.0, f"{selector} in {scheme} mode: contrast {ratio:.2f}, need >= 3.0"

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_bare_links_are_readable_against_their_background(self, page: Any, scheme: str) -> None:
        # Regression: "Export CSV" is a plain <a> with no rule of its own
        # matching it (unlike td a, or .topbar nav a) — it fell back to the
        # browser's unstyled default link colour, a fixed dark blue that
        # never adapted to this app's theme and read as barely legible
        # dark-blue-on-black once the surface actually went dark.
        page.emulate_media(color_scheme=scheme)
        _goto(page, "/audit")
        ratio = page.evaluate(_CONTRAST_RATIO_JS, "#export-csv")
        assert ratio >= 3.0, f"#export-csv in {scheme} mode: contrast {ratio:.2f}, need >= 3.0"


class TestEntityBrowser:
    def test_every_column_header_has_hover_help(self, page: Any) -> None:
        _goto(page, "/")
        headers = page.locator(".entity-table thead th")
        count = headers.count()
        assert count == 5
        for i in range(count):
            title = headers.nth(i).get_attribute("title")
            assert title, f"column {i} has no hover help"

    def test_lists_entities_with_their_sensor_type(self, page: Any) -> None:
        _goto(page, "/")
        row = page.locator("tr", has_text=ENTITY)
        expect(row).to_be_visible()
        expect(row.locator(".pill")).to_have_text("measurement")

    def test_search_filters_the_table(self, page: Any, adapter: FakeAdapter) -> None:
        adapter.add_entity("sensor.garden_humidity")
        _goto(page, "/")
        page.fill("#search", "garden")
        expect(page.locator("tr", has_text="sensor.garden_humidity")).to_be_visible()
        expect(page.locator("tr", has_text=ENTITY)).to_have_count(0)

    def test_an_entity_links_through_to_its_graph(self, page: Any) -> None:
        _goto(page, "/")
        page.click(f"a[href*='{ENTITY}']")
        page.wait_for_load_state("networkidle")
        expect(page.locator("h1")).to_contain_text(ENTITY)

    def test_the_entity_table_fits_a_phone_screen_without_page_overflow(self, page: Any) -> None:
        page.set_viewport_size({"width": 375, "height": 812})  # iPhone-sized
        _goto(page, "/")
        row = page.locator("tr", has_text=ENTITY)
        expect(row).to_be_visible()

        # The table's own .table-scroll wrapper may scroll internally, but the
        # page itself must never need a horizontal scrollbar — that was the
        # actual complaint (the table was "way too wide").
        body_width = page.evaluate("() => document.documentElement.scrollWidth")
        assert body_width <= 375

        # Below this width the table stops looking like a table at all — each
        # row becomes its own card, and the header (which only made sense
        # labelling columns side by side) disappears entirely.
        expect(page.locator("thead")).to_be_hidden()

        # Hidden at this width to make room, not merely scrolled off — it is
        # the lowest-priority field for finding and jumping into an entity.
        expect(row.locator("td[data-label='Last updated']")).to_be_hidden()

        # The remaining fields must still actually be there and readable —
        # stacking must not have silently dropped anything.
        expect(row.locator("td[data-label='Type']")).to_be_visible()
        expect(row.locator("td[data-label='Corrections']")).to_be_visible()

    def test_a_very_long_entity_name_does_not_break_the_tables_alignment(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        # Regression: auto table layout sizes each column to its widest cell —
        # one long, unbreakable entity_id stretched that whole column (and
        # therefore the table's width), shifting every other row out of
        # alignment with the header above it, however short their own values
        # were. A width just above the mobile stacking breakpoint (640px), so
        # this is testing the desktop table layout, not the stacked cards.
        page.set_viewport_size({"width": 700, "height": 900})
        long_id = "sensor." + "x" * 120
        adapter.add_entity(long_id)
        _goto(page, "/")
        row = page.locator("tr", has_text=long_id)
        expect(row).to_be_visible()

        table_width = page.evaluate(
            "() => document.querySelector('.entity-table').getBoundingClientRect().width"
        )
        # Fixed layout means every row's columns line up at the same x
        # position regardless of content — checking the header and the long
        # row's first column share a boundary is a direct check of exactly
        # what "alignment" means here, not just that nothing overflows.
        header_cell_right = page.evaluate(
            "() => document.querySelector('.entity-table thead th').getBoundingClientRect().right"
        )
        long_row_cell_right = page.evaluate(
            "(el) => el.getBoundingClientRect().right", row.locator("td").first.element_handle()
        )
        assert abs(header_cell_right - long_row_cell_right) < 1

        body_width = page.evaluate("() => document.documentElement.scrollWidth")
        assert body_width <= 700
        assert table_width <= 700

    def test_the_filter_survives_a_round_trip_through_the_topbar_nav(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        # Distinct from the "?from=" back-link tests below: this is the
        # topbar's own "Entities"/"Corrections" links, which carry no query
        # string of their own — a bare "/" navigation, exactly what used to
        # silently reset the search and type filter every time.
        adapter.add_entity("sensor.garden_humidity")
        _goto(page, "/")
        page.fill("#search", "garden")
        expect(page.locator("tr", has_text=ENTITY)).to_have_count(0)

        page.click(".topbar nav a:has-text('Corrections')")
        page.wait_for_load_state("networkidle")
        expect(page.locator("h1")).to_contain_text("Corrections")

        page.click(".topbar nav a:has-text('Entities')")
        page.wait_for_load_state("networkidle")
        expect(page.locator("#search")).to_have_value("garden")
        expect(page.locator("tr", has_text="sensor.garden_humidity")).to_be_visible()
        expect(page.locator("tr", has_text=ENTITY)).to_have_count(0)

    def test_back_to_entities_preserves_the_search_filter(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        # Regression: "Back to entities" used to be a plain link to "/",
        # always resetting to the first page with no filter — losing exactly
        # the search a user had just used to find the entity they clicked.
        adapter.add_entity("sensor.garden_humidity")
        _goto(page, "/")
        page.fill("#search", "garden")
        # Both entities are present in the unfiltered list, so waiting for
        # garden_humidity to appear proves nothing about whether the debounced
        # search has actually applied yet — waiting for the *other* entity to
        # disappear is what proves the filtered reload (and its URL update)
        # has completed.
        expect(page.locator("tr", has_text=ENTITY)).to_have_count(0)

        page.click("a[href*='sensor.garden_humidity']")
        page.wait_for_load_state("networkidle")
        expect(page.locator("h1")).to_contain_text("sensor.garden_humidity")

        page.click("#back-to-entities")
        page.wait_for_load_state("networkidle")
        expect(page.locator("#search")).to_have_value("garden")
        expect(page.locator("tr", has_text="sensor.garden_humidity")).to_be_visible()
        expect(page.locator("tr", has_text=ENTITY)).to_have_count(0)

    def test_back_to_entities_preserves_the_page_offset(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        # Regression: paging to page 2 and then clicking through to an entity
        # used to lose the offset the same way search was lost above — and,
        # separately, an earlier fix for this relied on document.referrer /
        # history.back(), which does not work at all once Home Assistant's
        # real Ingress panel renders the add-on inside an iframe (iframes get
        # a "no-referrer" policy, so referrer is empty there even though it
        # is populated in this test's plain, non-iframed page).
        for i in range(60):
            adapter.add_entity(f"sensor.extra_{i:02d}")
        _goto(page, "/")
        expect(page.locator("#page-info")).to_contain_text("1–50")

        page.click("#next")
        expect(page.locator("#page-info")).to_contain_text("51–")
        first_row = page.locator("#entity-rows tr").first
        first_row_text = first_row.inner_text()

        first_row.locator("a").first.click()
        page.wait_for_load_state("networkidle")
        expect(page.locator("h1")).to_be_visible()

        page.click("#back-to-entities")
        page.wait_for_load_state("networkidle")
        expect(page.locator("#page-info")).to_contain_text("51–")
        assert page.locator("#entity-rows tr").first.inner_text() == first_row_text

    def test_the_type_filter_narrows_the_table(self, page: Any, adapter: FakeAdapter) -> None:
        adapter.add_entity("sensor.energy_total", SensorType.COUNTER)
        adapter.add_statistics_metadata("sensor.energy_total", mean_type=0, has_sum=True)
        _goto(page, "/")
        expect(page.locator("tr", has_text=ENTITY)).to_be_visible()

        page.select_option("#type-filter", "counter")
        expect(page.locator("tr", has_text=ENTITY)).to_have_count(0)
        expect(page.locator("tr", has_text="sensor.energy_total")).to_be_visible()

    def test_clicking_a_sortable_header_toggles_direction(self, page: Any) -> None:
        _goto(page, "/")
        expect(page.locator("tr", has_text=ENTITY)).to_be_visible()

        header = page.locator("th.sortable[data-sort='entity_id']")
        page.click("th.sortable[data-sort='entity_id']")
        page.wait_for_load_state("networkidle")
        expect(header).to_have_class(re.compile(r"sort-desc"))

        page.click("th.sortable[data-sort='entity_id']")
        page.wait_for_load_state("networkidle")
        expect(header).to_have_class(re.compile(r"sort-asc"))

    def test_sorting_by_corrections_puts_the_corrected_entity_first(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        # Alphabetically this would sort before ENTITY; a correction-count
        # sort has to actually reorder the table, not just relabel a header.
        adapter.add_entity("sensor.aaa_first_alphabetically")
        adapter.apply_correction(
            entity_id=ENTITY,
            state_id=2,
            expected_original="-2000.0",
            new_value="20.2",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        _goto(page, "/")
        expect(page.locator("tr", has_text=ENTITY)).to_be_visible()

        page.click("th.sortable[data-sort='corrections']")
        page.wait_for_load_state("networkidle")
        first_row = page.locator("#entity-rows tr").first
        expect(first_row).to_contain_text(ENTITY)


class TestHiddenAttributeActuallyHides:
    """Every element that starts with the `hidden` attribute in the templates
    must actually render hidden — not just report `.hidden === true` on the
    DOM property, which a broken stylesheet can leave true while the element
    still paints.

    This exists because of a real bug: `.row` and `.readout` both set
    `display: flex` with no `[hidden]` exception, and an author stylesheet
    rule always beats the browser's own UA default for `[hidden]` — even at
    matching specificity — so every `.row`/`.readout` element rendered
    regardless of its `hidden` attribute. `p-cascade-ack-row` (a `.row`)
    showed the counter-cascade checkbox for every entity, permanently,
    whether or not it was ever a counter — reported as "the checkbox has no
    effect on Save", which traced back to this rather than to any JS logic.
    Checking `to_be_hidden()` (real rendered visibility) for every
    `hidden`-attributed element on first load, across both pages that use
    this pattern, catches the whole class rather than one element at a time.
    """

    def test_every_initially_hidden_element_on_the_entity_page_is_hidden(self, page: Any) -> None:
        _goto(page, f"/entity/{ENTITY}")
        expect(page.locator("#point-count")).to_contain_text("points plotted")
        ids = page.evaluate("() => [...document.querySelectorAll('[hidden]')].map((el) => el.id)")
        assert ids, "expected at least one hidden element on the entity page"
        for element_id in ids:
            expect(page.locator(f"#{element_id}")).to_be_hidden()

    def test_every_hidden_element_inside_the_open_correction_panel_is_hidden(
        self, page: Any
    ) -> None:
        # A hidden descendant nested inside another correctly-hidden ancestor
        # (#panel, whose own `hidden` handling works) stays invisible either
        # way, masking a broken `hidden` rule on the descendant itself until
        # the ancestor becomes visible — exactly what let `.row`'s bug hide
        # from the check above. Opening the panel is what actually exposes
        # p-cascade-ack-row (a non-counter entity, so it should stay hidden
        # once the panel is open) to a real rendering check.
        _goto(page, f"/entity/{ENTITY}")
        expect(page.locator("#point-count")).to_contain_text("points plotted")
        found = page.evaluate(
            """() => {
                const chart = Chart.getChart(document.getElementById('chart'));
                const index = chart.data.datasets[0].data.findIndex((p) => p.y === -2000);
                if (index < 0) return false;
                chart.options.onClick(null, [{ index }], chart);
                return true;
            }"""
        )
        assert found
        expect(page.locator("#panel")).to_be_visible()
        ids = page.evaluate(
            "() => [...document.querySelectorAll('#panel [hidden]')].map((el) => el.id)"
        )
        assert ids, "expected at least one hidden element inside the open panel"
        for element_id in ids:
            expect(page.locator(f"#{element_id}")).to_be_hidden()

    def test_every_hidden_element_inside_the_open_bulk_panel_is_hidden(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        # Same masking risk as the correction panel above, for #bulk-panel's
        # own descendants (bulk-skip-row is a .readout, bulk-cascade-ack-row
        # is a .row — both classes that had this exact bug).
        entity_id = "sensor.hidden_check_bulk"
        adapter.add_entity(entity_id, SensorType.MEASUREMENT)
        adapter.add_statistics_metadata(entity_id, mean_type=1, has_sum=False)
        base = SEED_TIMESTAMPS[0] - 7200
        for i in range(12):
            adapter.add_state(entity_id, 600 + i, base + i * 300, "20.0")
        _goto(page, f"/entity/{entity_id}")
        expect(page.locator("#point-count")).to_contain_text("points plotted")
        page.click("#bulk-toggle")
        coords = page.evaluate(
            """([startTs, endTs]) => {
                const chart = Chart.getChart(document.getElementById('chart'));
                const rect = document.getElementById('chart').getBoundingClientRect();
                return {
                    x1: rect.left + chart.scales.x.getPixelForValue(startTs * 1000),
                    x2: rect.left + chart.scales.x.getPixelForValue(endTs * 1000),
                    y: rect.top + rect.height / 2,
                };
            }""",
            [base + 150, base + 3150],
        )
        page.mouse.move(coords["x1"], coords["y"])
        page.mouse.down()
        page.mouse.move(coords["x2"], coords["y"], steps=8)
        page.mouse.up()
        expect(page.locator("#bulk-panel")).to_be_visible()
        ids = page.evaluate(
            "() => [...document.querySelectorAll('#bulk-panel [hidden]')].map((el) => el.id)"
        )
        assert ids, "expected at least one hidden element inside the open bulk panel"
        for element_id in ids:
            expect(page.locator(f"#{element_id}")).to_be_hidden()

    def test_every_initially_hidden_element_on_the_audit_page_is_hidden(self, page: Any) -> None:
        _goto(page, "/audit")
        ids = page.evaluate("() => [...document.querySelectorAll('[hidden]')].map((el) => el.id)")
        for element_id in ids:
            if not element_id:
                continue
            expect(page.locator(f"#{element_id}")).to_be_hidden()


class TestGraphPage:
    def test_points_are_at_their_final_position_immediately_no_entrance_animation(
        self, page: Any
    ) -> None:
        # Chart.js's default ~1000ms entrance animation re-runs on every
        # range change and every prev/next page, not just the first load —
        # and hit-testing happens against wherever a point currently is
        # mid-animation, so a real click right after the data changes could
        # land on the wrong point, or none, on a slower device before it
        # settles. Verified directly: a point's rendered position is
        # compared immediately after load against its own position a moment
        # later — if animation were still running, those would differ.
        _goto(page, f"/entity/{ENTITY}")
        expect(page.locator("#point-count")).to_contain_text("points plotted")
        immediately, later = page.evaluate(
            """() => new Promise((resolve) => {
                const chart = Chart.getChart(document.getElementById('chart'));
                const meta = () => {
                    const m = chart.getDatasetMeta(0).data[0];
                    return { x: m.x, y: m.y };
                };
                const first = meta();
                setTimeout(() => resolve([first, meta()]), 300);
            })"""
        )
        assert immediately == later, (
            f"point moved after render ({immediately} -> {later}) — animation is not off"
        )

    def test_the_30_day_axis_labels_do_not_overlap_on_a_phone_screen(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        # Reported: on an iPhone-width screen, the 30-day view's date labels
        # ("28 Aug 2026") ran into each other. Confirmed by measuring actual
        # rendered label bounding boxes (canvas text has no DOM box to read
        # directly) at several phone widths — Chart.js's own autoSkip left
        # adjacent labels overlapping by up to 14px, using its default 3px
        # autoSkipPadding, too tight for this axis's actual label width.
        # Checks the real rendered gap between every adjacent pair of ticks,
        # not just that *a* fix was applied, so a future change that
        # reintroduces crowding — e.g. a longer date format — fails this too.
        entity_id = "sensor.month_of_data"
        adapter.add_entity(entity_id, SensorType.MEASUREMENT)
        now = time.time()
        for i in range(32 * 24):
            adapter.add_state(entity_id, 2000 + i, now - i * 3600, "20.0")
        page.set_viewport_size({"width": 375, "height": 812})
        _goto(page, f"/entity/{entity_id}")
        page.select_option("#range", "2592000")
        page.wait_for_load_state("networkidle")

        boxes = page.evaluate(
            """() => {
                const chart = Chart.getChart(document.getElementById('chart'));
                const scale = chart.scales.x;
                const ticks = scale.ticks;
                const ctx = chart.ctx;
                const computed = getComputedStyle(chart.canvas);
                ctx.font = `${computed.fontSize} ${computed.fontFamily}`;
                return ticks.map((t, i) => {
                    const x = scale.getPixelForTick(i);
                    const w = ctx.measureText(t.label).width;
                    return { label: t.label, left: x - w / 2, right: x + w / 2 };
                });
            }"""
        )
        assert len(boxes) >= 2, "expected more than one tick on a 30-day view"
        for prev, cur in itertools.pairwise(boxes):
            gap = cur["left"] - prev["right"]
            assert gap >= 0, f"{prev['label']!r} overlaps {cur['label']!r} by {-gap:.1f}px"

    def test_the_header_row_wraps_instead_of_overflowing_on_a_phone_screen(self, page: Any) -> None:
        # Regression: .spread (title block + sensor-type pill + "Back to
        # entities") had no flex-wrap, so a long entity_id squeezed against
        # the pill and button overflowed the screen instead of stacking —
        # unlike the entities and corrections tables, which stack cleanly.
        page.set_viewport_size({"width": 375, "height": 812})
        _goto(page, f"/entity/{ENTITY}")
        expect(page.locator("#back-to-entities")).to_be_visible()

        body_width = page.evaluate("() => document.documentElement.scrollWidth")
        assert body_width <= 375

    def test_a_long_entity_id_wraps_instead_of_overflowing_the_heading(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        # Regression: flex-wrap on .spread (fixed above) only lets the *row*
        # wrap — the <h1> and the entity_id subtitle beneath it are each a
        # single long unbroken string with no spaces, which overflows its own
        # container width rather than wrapping onto a second line regardless
        # of whether the row around it wraps.
        long_id = "sensor." + "x" * 60
        adapter.add_entity(long_id)
        page.set_viewport_size({"width": 375, "height": 812})
        _goto(page, f"/entity/{long_id}")
        expect(page.locator("h1")).to_contain_text(long_id)

        body_width = page.evaluate("() => document.documentElement.scrollWidth")
        assert body_width <= 375

    def test_a_slow_period_change_shows_a_visible_loading_state(
        self, page: Any, adapter: FakeAdapter, monkeypatch: Any
    ) -> None:
        # The chart canvas does not clear while new data loads, so on a fast
        # connection switching period could look like nothing happened until
        # the new data suddenly appears — this is the regression that
        # complaint described. Delaying the server's own response (rather
        # than intercepting the request in Playwright) is what makes the
        # in-flight window long enough to assert on: Playwright's sync API
        # cannot resume an intercepted route from a background thread without
        # blocking its own driver thread, which would stall this test's
        # assertions right along with the request.
        real_fetch_states = adapter.fetch_states

        def _slow_fetch_states(*args: Any, **kwargs: Any) -> Any:
            time.sleep(0.5)
            return real_fetch_states(*args, **kwargs)

        monkeypatch.setattr(adapter, "fetch_states", _slow_fetch_states)

        _goto(page, f"/entity/{ENTITY}")
        expect(page.locator("#point-count")).to_contain_text("points plotted")

        page.click("#range-prev")
        expect(page.locator("#chart-loading")).to_be_visible()
        expect(page.locator("#range-prev")).to_be_disabled()
        expect(page.locator("#range-next")).to_be_disabled()
        expect(page.locator("#range")).to_be_disabled()

        page.wait_for_load_state("networkidle")
        expect(page.locator("#chart-loading")).to_be_hidden()
        expect(page.locator("#range")).to_be_enabled()

    def test_the_chart_renders_with_the_loaded_points(self, page: Any) -> None:
        _goto(page, f"/entity/{ENTITY}")
        expect(page.locator("#chart")).to_be_visible()
        # The count only appears once load() has finished and the dataset is in.
        expect(page.locator("#point-count")).to_contain_text("points plotted")

        plotted = page.evaluate(
            "() => Chart.getChart(document.getElementById('chart')).data.datasets[0].data.length"
        )
        assert plotted == 3

    def test_non_numeric_points_are_explained_not_just_counted(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        # "19974 of 20000 points plotted" alone does not say why 26 are
        # missing — they are real "unknown"/"unavailable" readings (a
        # dropout, an integration restart), shown as gaps rather than points,
        # not a bug losing data.
        adapter.add_state(ENTITY, 900, SEED_TIMESTAMPS[2] + 100, "unknown")
        adapter.add_state(ENTITY, 901, SEED_TIMESTAMPS[2] + 200, "unavailable")
        _goto(page, f"/entity/{ENTITY}")
        expect(page.locator("#point-count")).to_contain_text("2 non-numeric, shown as gaps")

    def test_backfilled_points_are_shown_explained_and_not_clickable(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        # A range reaching further back than raw states survive backfills
        # from long-term statistics — see long-term-statistics-graph-design.md.
        # The web-layer merge itself is covered by test_web.py's
        # TestStatesApiBackfill; this confirms the frontend actually renders
        # the result distinctly, explains it, and refuses to open the
        # correction panel for one.
        from hr_statistics import HOURLY_SECONDS

        entity_id = "sensor.long_history"
        adapter.add_entity(entity_id, SensorType.MEASUREMENT)
        adapter.add_statistics_metadata(entity_id, mean_type=1, has_sum=False)
        earliest_state_ts = SEED_TIMESTAMPS[0]
        adapter.add_state(entity_id, 900, earliest_state_ts, "20.0")
        adapter.add_hourly_statistics(entity_id, earliest_state_ts - HOURLY_SECONDS, mean=15.0)

        _goto(page, f"/entity/{entity_id}")
        page.select_option("#range", "2592000")  # 30 days, reaches past the seeded gap
        page.wait_for_timeout(300)

        expect(page.locator("#backfill-notice")).to_be_visible()
        expect(page.locator("#backfill-notice")).to_contain_text("hourly averages")
        expect(page.locator("#backfill-notice")).to_contain_text("not corrected")

        sources = page.evaluate(
            "() => Chart.getChart(document.getElementById('chart'))"
            ".data.datasets[0].data.map(p => p.source)"
        )
        assert "statistics" in sources
        assert "state" in sources

        # Clicking the backfilled point must not open the correction panel —
        # there is nothing there to correct.
        index = sources.index("statistics")
        page.evaluate(
            f"""() => {{
                const chart = Chart.getChart(document.getElementById('chart'));
                const meta = chart.getDatasetMeta(0);
                const rect = meta.data[{index}].getProps(['x', 'y'], true);
                const canvas = document.getElementById('chart');
                const box = canvas.getBoundingClientRect();
                canvas.dispatchEvent(new MouseEvent('click', {{
                    clientX: box.left + rect.x, clientY: box.top + rect.y, bubbles: true,
                }}));
            }}"""
        )
        expect(page.locator("#panel")).to_be_hidden()
        expect(page.locator("#toast")).to_contain_text("not corrected")

    def test_the_statistics_notice_is_shown(self, page: Any) -> None:
        # Corrections now rebuild the statistics buckets as well, but Home
        # Assistant caches statistics in memory, so the user has to be told a
        # restart may be needed before they conclude nothing happened.
        _goto(page, f"/entity/{ENTITY}")
        expect(page.locator(".notice", has_text="Statistics are corrected too")).to_be_visible()
        expect(page.locator(".notice", has_text="restart Home Assistant")).to_be_visible()

    def test_a_counter_sensor_warns_about_its_running_totals(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        adapter.add_entity("sensor.energy_total", SensorType.COUNTER)
        adapter.add_statistics_metadata("sensor.energy_total", mean_type=0, has_sum=True)
        adapter.add_state("sensor.energy_total", 9, 1_700_000_000.0, "999999.0")
        _goto(page, "/entity/sensor.energy_total")
        expect(page.locator(".notice", has_text="This is a counter")).to_be_visible()

    def test_truncation_is_announced_when_the_range_holds_more(
        self, page: Any, adapter: FakeAdapter, monkeypatch: Any
    ) -> None:
        from hr_models import StateSeries

        real = adapter.fetch_states

        def _small(entity_id: str, start: float, end: float, limit: int = 20000) -> StateSeries:
            return real(entity_id, start, end, limit=2)

        monkeypatch.setattr(adapter, "fetch_states", _small)
        _goto(page, f"/entity/{ENTITY}")
        notice = page.locator("#truncation-notice")
        expect(notice).to_be_visible()
        expect(notice).to_contain_text("there are more")

    def test_the_seeded_outlier_is_highlighted_as_a_candidate(self, page: Any) -> None:
        _goto(page, f"/entity/{ENTITY}")
        expect(page.locator("#point-count")).to_contain_text("flagged")

        flagged = page.evaluate(
            "() => Chart.getChart(document.getElementById('chart'))"
            ".data.datasets[0].data.filter((p) => p.candidate).map((p) => p.y)"
        )
        assert flagged == [-2000.0]

    def test_the_chart_uses_a_fixed_date_time_format(self, page: Any) -> None:
        # Not locale-derived: relying on the ambient browser/WebView locale
        # was tried and found to render US-style 12-hour dates inside Home
        # Assistant's own Ingress panel for a viewer whose system was actually
        # set to 24-hour, day-month-year — the browser's default locale is not
        # a reliable stand-in for the viewer's real preference. A fixed format
        # is deterministic and correct everywhere instead.
        _goto(page, f"/entity/{ENTITY}")
        options = page.evaluate("() => Chart.getChart(document.getElementById('chart')).options")
        assert options["scales"]["x"]["time"]["tooltipFormat"] == "d MMM yyyy, HH:mm:ss"

    def test_the_axis_ticks_include_a_year_like_every_other_date_in_the_app(
        self, page: Any
    ) -> None:
        # Before this, a 7- or 30-day graph's day/week axis ticks read
        # "28 Aug" — no year — while hovering the same point, the correction
        # panel, and every other date in the app always show one. The one
        # place a date looked like a different, shorter format than
        # everywhere else.
        _goto(page, f"/entity/{ENTITY}")
        display_formats = page.evaluate(
            "() => Chart.getChart(document.getElementById('chart'))"
            ".options.scales.x.time.displayFormats"
        )
        assert display_formats["day"] == "d MMM yyyy"
        assert display_formats["week"] == "d MMM yyyy"
        assert display_formats["month"] == "MMM yyyy"

    def test_dragging_sensitivity_reloads_with_the_new_threshold(self, page: Any) -> None:
        # Proof the slider reaches the API at all, independent of any
        # particular fixture's deviation numbers.
        _goto(page, f"/entity/{ENTITY}")
        expect(page.locator("#point-count")).to_contain_text("flagged")

        slider = page.locator("#sensitivity")
        with page.expect_request(lambda r: "threshold=6.5" in r.url):
            slider.fill("6.5")
            slider.dispatch_event("input")
        expect(page.locator("#sensitivity-value")).to_have_text("6.5")

    def test_raising_sensitivity_clears_a_borderline_candidate(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        # A moderate deviation (4.5) that the default threshold (3.0) flags
        # and a higher one (5.0) does not — chosen by running the detector
        # directly, not guessed. Confirms the slider changes what gets
        # flagged, not merely that a request goes out.
        adapter.add_entity("sensor.borderline", SensorType.MEASUREMENT)
        adapter.add_statistics_metadata("sensor.borderline", mean_type=1, has_sum=False)
        # Comfortably in the past: SEED_TIMESTAMPS[0] alone is only 1800s
        # before module import, and the last of 9 points 300s apart would
        # land in the future by the time the browser actually loads the page.
        base_ts = SEED_TIMESTAMPS[0] - 3600
        for i, value in enumerate([9.0, 11.0, 9.5, 10.5, 9.0, 11.0, 9.5, 10.5, 15.0]):
            adapter.add_state("sensor.borderline", 100 + i, base_ts + i * 300, str(value))

        _goto(page, "/entity/sensor.borderline")
        expect(page.locator("#point-count")).to_contain_text("1 flagged")

        slider = page.locator("#sensitivity")
        with page.expect_request(lambda r: "threshold=5" in r.url):
            slider.fill("5")
            slider.dispatch_event("input")
        expect(page.locator("#point-count")).not_to_contain_text("flagged")


class TestRangeNavigation:
    """Paging the graph window further back than the longest preset (30d)."""

    def test_next_is_disabled_at_the_latest_window(self, page: Any) -> None:
        # The seeded history sits within the last hour, so the default (most
        # recent) window is already showing "now" — there is nothing later.
        _goto(page, f"/entity/{ENTITY}")
        expect(page.locator("#range-next")).to_be_disabled()

    def test_prev_pages_to_an_earlier_empty_window_and_back(self, page: Any) -> None:
        _goto(page, f"/entity/{ENTITY}")
        expect(page.locator("#point-count")).to_contain_text("points plotted")

        page.click("#range-prev")
        page.wait_for_load_state("networkidle")
        # A window a full range-width earlier holds none of the seeded data,
        # which sits within the last hour.
        expect(page.locator("#empty-notice")).to_be_visible()
        expect(page.locator("#range-next")).to_be_enabled()

        page.click("#range-next")
        page.wait_for_load_state("networkidle")
        expect(page.locator("#point-count")).to_contain_text("points plotted")
        expect(page.locator("#range-next")).to_be_disabled()

    def test_changing_the_range_jumps_back_to_the_latest_window(self, page: Any) -> None:
        _goto(page, f"/entity/{ENTITY}")
        page.click("#range-prev")
        page.wait_for_load_state("networkidle")
        expect(page.locator("#empty-notice")).to_be_visible()

        page.select_option("#range", "86400")
        page.wait_for_load_state("networkidle")
        expect(page.locator("#point-count")).to_contain_text("points plotted")
        expect(page.locator("#range-next")).to_be_disabled()


class TestCorrectionFlow:
    """The path a user actually takes: spot the bad point, fix it, undo it."""

    def _open_panel(self, page: Any, match: str = "p.y === -2000") -> None:
        """Open the correction panel for the first point matching `match`.

        Selection goes through the chart's own onClick handler rather than
        reaching into the page's internals, so the wiring between Chart.js and
        the panel is genuinely under test. `match` is a JavaScript expression
        over a datapoint `p`; once a point has been corrected its value is no
        longer the outlier, so callers switch to `p.corrected`.
        """
        _goto(page, f"/entity/{ENTITY}")
        expect(page.locator("#point-count")).to_contain_text("points plotted")
        found = page.evaluate(
            """(match) => {
                const chart = Chart.getChart(document.getElementById('chart'));
                const index = chart.data.datasets[0].data
                    .findIndex((p) => eval(match));
                if (index < 0) return false;
                chart.options.onClick(null, [{ index }], chart);
                return true;
            }""",
            match,
        )
        assert found, f"no datapoint matched {match!r}"
        expect(page.locator("#panel")).to_be_visible()

    def test_clicking_a_point_shows_its_recorded_value(self, page: Any) -> None:
        self._open_panel(page)
        expect(page.locator("#p-value")).to_have_text("-2000.0")

    def test_the_cascade_checkbox_is_actually_hidden_for_a_non_counter_entity(
        self, page: Any
    ) -> None:
        # `#p-cascade-ack-row` uses both `class="row"` (display: flex, no
        # exception for [hidden]) and the `hidden` attribute — an author
        # stylesheet rule always beats the browser's own UA default for
        # [hidden], even at matching specificity, so `.row { display: flex }`
        # alone silently overrode it: the DOM's `hidden` property read `true`
        # correctly, but the checkbox rendered anyway, permanently, for
        # every entity regardless of whether it was ever a counter. A
        # reported "the cascade checkbox has no effect on Save" traced back
        # to exactly this — the entity being tested was never a counter, so
        # Save being enabled was already correct, and the visible checkbox
        # was the actual bug. `to_be_hidden()` checks real rendered
        # visibility, unlike reading `.hidden` off the element directly,
        # which would have passed even with the bug present.
        self._open_panel(page)
        expect(page.locator("#p-cascade-ack-row")).to_be_hidden()

    def test_correcting_a_point_updates_the_graph(self, page: Any, adapter: FakeAdapter) -> None:
        self._open_panel(page)
        page.fill("#p-new", "20.2")
        page.select_option("#p-quality", "bad_comm")
        page.fill("#p-note", "modem reboot")
        page.click("#p-save")

        expect(page.locator("#toast")).to_contain_text("Correction saved")
        expect(page.locator("#point-count")).to_contain_text("1 corrected")

        # The write really reached the database, not just the UI.
        assert adapter.states[2][2] == "20.2"
        correction = next(iter(adapter.corrections.values()))
        assert correction.note == "modem reboot"
        assert correction.quality.value == "bad_comm"

    def test_a_non_numeric_value_is_rejected_with_a_message(self, page: Any) -> None:
        self._open_panel(page)
        page.fill("#p-new", "twenty")
        page.click("#p-save")
        expect(page.locator("#toast")).to_contain_text("not a number")

    def test_a_corrected_point_offers_restore_instead_of_a_second_correction(
        self, page: Any
    ) -> None:
        self._open_panel(page)
        page.fill("#p-new", "20.2")
        page.click("#p-save")
        expect(page.locator("#point-count")).to_contain_text("1 corrected")

        self._open_panel(page, "p.corrected")
        # Correcting again would record the correction as the "original" and
        # lose the real one, so the form is replaced by a restore button.
        expect(page.locator("#p-restore")).to_be_visible()
        expect(page.locator("#p-form")).to_be_hidden()
        expect(page.locator("#p-orig")).to_have_text("-2000.0")

    def test_restore_puts_the_original_value_back(self, page: Any, adapter: FakeAdapter) -> None:
        self._open_panel(page)
        page.fill("#p-new", "20.2")
        page.click("#p-save")
        expect(page.locator("#point-count")).to_contain_text("1 corrected")

        self._open_panel(page, "p.corrected")
        page.click("#p-restore")
        expect(page.locator("#toast")).to_contain_text("restored")
        assert adapter.states[2][2] == "-2000.0"

    def test_pressing_enter_before_the_cascade_is_acknowledged_does_not_submit(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        # The Save button is disabled until the counter cascade checkbox is
        # ticked — but a disabled submit button does not necessarily stop a
        # <form> from submitting via the Enter key in a text field, which is
        # a separate browser mechanism (implicit submission). If it did fire
        # here, a counter correction would go through without the user ever
        # having seen or acknowledged how many statistics rows it rewrites.
        adapter.add_entity("sensor.energy_total", SensorType.COUNTER)
        adapter.add_statistics_metadata("sensor.energy_total", mean_type=0, has_sum=True)
        adapter.add_state("sensor.energy_total", 9, SEED_TIMESTAMPS[0], "999999.0")
        _goto(page, "/entity/sensor.energy_total")
        expect(page.locator("#point-count")).to_contain_text("points plotted")
        found = page.evaluate(
            """() => {
                const chart = Chart.getChart(document.getElementById('chart'));
                const index = chart.data.datasets[0].data.findIndex((p) => p.y === 999999.0);
                if (index < 0) return false;
                chart.options.onClick(null, [{ index }], chart);
                return true;
            }"""
        )
        assert found
        expect(page.locator("#panel")).to_be_visible()
        expect(page.locator("#p-save")).to_be_disabled()
        expect(page.locator("#p-cascade-ack")).not_to_be_checked()

        page.fill("#p-new", "1100.0")
        page.fill("#p-note", "test")
        page.press("#p-note", "Enter")

        # Give any submission a moment to actually reach the server before
        # asserting nothing happened.
        page.wait_for_timeout(300)
        assert "sensor.energy_total" not in [c.entity_id for c in adapter.corrections.values()]
        assert adapter.states[9][2] == "999999.0"
        expect(page.locator("#panel")).to_be_visible()

    def test_an_unknown_sensor_type_can_still_be_corrected(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        # No statistics_meta row at all, so no cascade — but the state value
        # itself is still correctable, per the "states-only" notice.
        entity_id = "sensor.mystery"
        adapter.add_entity(entity_id, SensorType.UNKNOWN)
        adapter.add_state(entity_id, 900, SEED_TIMESTAMPS[0], "5.0")

        _goto(page, f"/entity/{entity_id}")
        expect(page.get_by_text("States-only correction")).to_be_visible()

        found = page.evaluate(
            """() => {
                const chart = Chart.getChart(document.getElementById('chart'));
                const index = chart.data.datasets[0].data.findIndex((p) => p.y === 5.0);
                if (index < 0) return false;
                chart.options.onClick(null, [{ index }], chart);
                return true;
            }"""
        )
        assert found
        expect(page.locator("#panel")).to_be_visible()

        page.fill("#p-new", "6.0")
        page.click("#p-save")
        expect(page.locator("#toast")).to_contain_text("Correction saved")
        assert adapter.states[900][2] == "6.0"


class TestBulkCorrectionFlow:
    """Drag across the graph to select a range, then fill it in one action."""

    BULK_ENTITY = "sensor.bulk_drag_test"

    def _seed(self, adapter: FakeAdapter) -> float:
        """Twelve 5-minute readings; returns the base timestamp."""
        adapter.add_entity(self.BULK_ENTITY, SensorType.MEASUREMENT)
        adapter.add_statistics_metadata(self.BULK_ENTITY, mean_type=1, has_sum=False)
        base = SEED_TIMESTAMPS[0] - 7200  # comfortably in the past
        for i in range(12):
            value = "-999.0" if 4 <= i <= 7 else "20.0"
            adapter.add_state(self.BULK_ENTITY, 500 + i, base + i * 300, value)
        return base

    def _drag_select(self, page: Any, start_ts: float, end_ts: float) -> None:
        """Drag on the real canvas between the pixels for these timestamps.

        Goes through the actual mousedown/mousemove/mouseup flow the feature
        is built on, rather than calling its JS handlers directly — the pixel
        positions are computed from the chart's own scale so the drag lands
        exactly on the intended range regardless of chart size.

        Callers should land start_ts/end_ts strictly between two readings,
        not exactly on one: getPixelForValue/getValueForPixel round to whole
        pixels, so a boundary chosen exactly at a data point's timestamp can
        round to either side of it and include one extra or one fewer row.
        A real drag lands between points anyway, so this matches actual use.
        """
        coords = page.evaluate(
            """([startTs, endTs]) => {
                const chart = Chart.getChart(document.getElementById('chart'));
                const rect = document.getElementById('chart').getBoundingClientRect();
                return {
                    x1: rect.left + chart.scales.x.getPixelForValue(startTs * 1000),
                    x2: rect.left + chart.scales.x.getPixelForValue(endTs * 1000),
                    y: rect.top + rect.height / 2,
                };
            }""",
            [start_ts, end_ts],
        )
        page.mouse.move(coords["x1"], coords["y"])
        page.mouse.down()
        page.mouse.move(coords["x2"], coords["y"], steps=8)
        page.mouse.up()

    def test_toggling_range_mode_updates_the_hint(self, page: Any, adapter: FakeAdapter) -> None:
        self._seed(adapter)
        _goto(page, f"/entity/{self.BULK_ENTITY}")
        expect(page.locator("#chart-hint")).to_contain_text("Click a point")
        page.click("#bulk-toggle")
        expect(page.locator("#chart-hint")).to_contain_text("Drag across")
        expect(page.locator("#chart-wrap")).to_have_class("chart-wrap bulk-mode")

    def test_dragging_a_range_opens_the_bulk_panel_with_a_preview(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        base = self._seed(adapter)
        _goto(page, f"/entity/{self.BULK_ENTITY}")
        expect(page.locator("#point-count")).to_contain_text("points plotted")

        page.click("#bulk-toggle")
        # Indices 4..7 are the bad readings; select comfortably around them.
        self._drag_select(page, base + 3.5 * 300, base + 7.5 * 300)

        expect(page.locator("#bulk-panel")).to_be_visible()
        expect(page.locator("#bulk-total")).to_have_text("4")

    def test_a_short_click_does_not_open_the_panel(self, page: Any, adapter: FakeAdapter) -> None:
        base = self._seed(adapter)
        _goto(page, f"/entity/{self.BULK_ENTITY}")
        page.click("#bulk-toggle")
        self._drag_select(page, base + 3 * 300, base + 3 * 300)
        expect(page.locator("#bulk-panel")).to_be_hidden()

    def test_constant_strategy_corrects_the_dragged_range(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        base = self._seed(adapter)
        _goto(page, f"/entity/{self.BULK_ENTITY}")
        page.click("#bulk-toggle")
        self._drag_select(page, base + 3.5 * 300, base + 7.5 * 300)
        expect(page.locator("#bulk-panel")).to_be_visible()

        page.select_option("#bulk-strategy", "constant")
        page.fill("#bulk-value", "20.5")
        page.select_option("#bulk-quality", "bad_comm")
        page.click("#bulk-save")

        expect(page.locator("#toast")).to_contain_text("Corrected 4 readings")
        expect(page.locator("#bulk-panel")).to_be_hidden()
        # Bulk mode exits automatically after a successful save.
        expect(page.locator("#chart-hint")).to_contain_text("Click a point")

        for i in (4, 5, 6, 7):
            assert adapter.states[500 + i][2] == "20.5"
        for i in (0, 1, 2, 3, 8, 9, 10, 11):
            assert adapter.states[500 + i][2] == "20.0"

    def test_interpolate_strategy_ramps_between_the_edges(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        base = self._seed(adapter)
        # Distinct anchor values either side of the bad range, so a ramp is
        # visibly different from a flat correction.
        adapter.states[503] = (self.BULK_ENTITY, base + 3 * 300, "10.0")
        adapter.states[508] = (self.BULK_ENTITY, base + 8 * 300, "30.0")

        _goto(page, f"/entity/{self.BULK_ENTITY}")
        page.click("#bulk-toggle")
        self._drag_select(page, base + 3.5 * 300, base + 7.5 * 300)
        expect(page.locator("#bulk-panel")).to_be_visible()

        page.click("#bulk-save")  # interpolate is the default strategy
        expect(page.locator("#toast")).to_contain_text("Corrected 4 readings")

        values = [float(adapter.states[500 + i][2]) for i in (4, 5, 6, 7)]
        assert values == sorted(values)
        assert 10.0 < values[0] < values[-1] < 30.0

    def test_closing_the_panel_discards_the_selection(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        base = self._seed(adapter)
        _goto(page, f"/entity/{self.BULK_ENTITY}")
        page.click("#bulk-toggle")
        self._drag_select(page, base + 3.5 * 300, base + 7.5 * 300)
        expect(page.locator("#bulk-panel")).to_be_visible()

        page.click("#bulk-panel-close")
        expect(page.locator("#bulk-panel")).to_be_hidden()
        for i in (4, 5, 6, 7):
            assert adapter.states[500 + i][2] == "-999.0"

    def test_a_counter_range_shows_the_cascade_warning_and_gates_saving(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        adapter.add_entity("sensor.bulk_counter_drag", SensorType.COUNTER)
        adapter.add_statistics_metadata("sensor.bulk_counter_drag", mean_type=0, has_sum=True)
        base = SEED_TIMESTAMPS[0] - 7200
        values = ["100.0", "105.0", "99999.0", "99999.0", "120.0", "125.0"]
        for i, value in enumerate(values):
            adapter.add_state("sensor.bulk_counter_drag", 600 + i, base + i * 300, value)

        _goto(page, "/entity/sensor.bulk_counter_drag")
        page.click("#bulk-toggle")
        self._drag_select(page, base + 1.5 * 300, base + 3.5 * 300)
        expect(page.locator("#bulk-panel")).to_be_visible()
        expect(page.locator("#bulk-cascade-warning")).to_be_visible()

        # The save button stays disabled until the cascade is acknowledged.
        page.select_option("#bulk-strategy", "constant")
        page.fill("#bulk-value", "110.0")
        expect(page.locator("#bulk-save")).to_be_disabled()
        page.check("#bulk-cascade-ack")
        expect(page.locator("#bulk-save")).to_be_enabled()

        page.click("#bulk-save")
        expect(page.locator("#toast")).to_contain_text("Corrected")


class TestAuditPage:
    def test_every_column_header_has_hover_help(self, page: Any) -> None:
        _goto(page, "/audit")
        headers = page.locator(".audit-table thead th")
        # The last header is the empty restore-action column; skip it since
        # a header labelling no data has nothing to explain.
        count = headers.count()
        assert count == 7
        for i in range(count - 1):
            title = headers.nth(i).get_attribute("title")
            assert title, f"column {i} has no hover help"

    def test_lists_a_correction_and_restores_it(self, page: Any, adapter: FakeAdapter) -> None:
        from hr_models import Quality

        adapter.apply_correction(
            entity_id=ENTITY,
            state_id=2,
            expected_original="-2000.0",
            new_value="20.2",
            quality=Quality.SPIKE,
            note="from a test",
            created_by="pytest",
        )

        _goto(page, "/audit")
        row = page.locator("tr", has_text=ENTITY)
        expect(row).to_contain_text("-2000.0")
        expect(row).to_contain_text("20.2")
        expect(row).to_contain_text("Spike")

        page.click("button[data-restore]")
        expect(page.locator("#toast")).to_contain_text("restored")
        assert adapter.states[2][2] == "-2000.0"

    def test_the_restored_timestamp_is_shown_in_local_time_not_a_raw_iso_string(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        # created_at/restored_at/dismissed_at come from the database as
        # `.isoformat()` strings, not epoch seconds like state_ts — and
        # MariaDB/PostgreSQL hand back a naive driver datetime with no
        # timezone offset at all ("2026-01-02T00:00:00" — FakeAdapter mirrors
        # this exact shape). The audit page used to print that string
        # straight onto the page: a viewer saw raw UTC ISO text instead of
        # their own local time, in a visibly different format from every
        # other date in the app.
        from hr_models import Quality

        adapter.apply_correction(
            entity_id=ENTITY,
            state_id=2,
            expected_original="-2000.0",
            new_value="20.2",
            quality=Quality.SPIKE,
            note="from a test",
            created_by="pytest",
        )
        _goto(page, "/audit")
        page.click("button[data-restore]")
        expect(page.locator("#toast")).to_contain_text("restored")

        row = page.locator("tr", has_text=ENTITY)
        expect(row).to_contain_text("Restored")
        text = row.inner_text()
        assert "2026-01-02T00:00:00" not in text
        assert re.search(r"\d{1,2} \w{3} 2026", text), text

    @pytest.mark.parametrize("width", [700, 900, 1024, 1280])
    def test_the_restore_button_fits_inside_its_own_cell(
        self, page: Any, adapter: FakeAdapter, width: int
    ) -> None:
        # The action column was sized with a percentage (6% of the table's
        # width), which shrinks along with the table — the Restore button
        # inside it does not, since it has its own fixed padding and text.
        # Measured directly: the button rendered 82.5px wide while a 6%
        # column gave it only 67.8px, clipping roughly 15px of it against
        # `.table-scroll`'s `overflow-x: hidden`, on an ordinary desktop
        # window (not a phone, where the table stacks into cards instead).
        from hr_models import Quality

        adapter.apply_correction(
            entity_id=ENTITY,
            state_id=2,
            expected_original="-2000.0",
            new_value="20.2",
            quality=Quality.SPIKE,
            note="from a test",
            created_by="pytest",
        )
        page.set_viewport_size({"width": width, "height": 900})
        _goto(page, "/audit")
        fits = page.evaluate("""() => {
            const btn = document.querySelector('[data-restore]');
            const td = btn.closest('td');
            return btn.getBoundingClientRect().right <= td.getBoundingClientRect().right + 0.5;
        }""")
        assert fits, f"Restore button overflows its cell at {width}px"

    def test_a_very_long_entity_name_does_not_break_the_tables_alignment(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        from hr_models import Quality

        # Same fix as the entity browser's table, and the same reason it is
        # needed here too: this table's first column is also an entity_id,
        # just as capable of being one long unbreakable string.
        long_id = "sensor." + "x" * 120
        adapter.add_entity(long_id)
        adapter.add_state(long_id, 99, 1_700_000_000.0, "1.0")
        adapter.apply_correction(
            entity_id=long_id,
            state_id=99,
            expected_original="1.0",
            new_value="2.0",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        page.set_viewport_size({"width": 700, "height": 900})
        _goto(page, "/audit")
        row = page.locator("tr", has_text=long_id)
        expect(row).to_be_visible()

        header_cell_right = page.evaluate(
            "() => document.querySelector('.audit-table thead th').getBoundingClientRect().right"
        )
        long_row_cell_right = page.evaluate(
            "(el) => el.getBoundingClientRect().right", row.locator("td").first.element_handle()
        )
        assert abs(header_cell_right - long_row_cell_right) < 1

        body_width = page.evaluate("() => document.documentElement.scrollWidth")
        assert body_width <= 700

    def test_the_table_stacks_into_cards_on_a_phone_screen(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        from hr_models import Quality

        adapter.apply_correction(
            entity_id=ENTITY,
            state_id=2,
            expected_original="-2000.0",
            new_value="20.2",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        page.set_viewport_size({"width": 375, "height": 812})
        _goto(page, "/audit")
        row = page.locator("tr", has_text=ENTITY)
        expect(row).to_be_visible()

        body_width = page.evaluate("() => document.documentElement.scrollWidth")
        assert body_width <= 375
        expect(page.locator("thead")).to_be_hidden()

        # Every field survives stacking, not just the ones that happened to
        # fit — including "Reason", which has no data-label of its own value
        # visible in a real table row without the header above it.
        expect(row.locator("td[data-label='Original']")).to_contain_text("-2000.0")
        expect(row.locator("td[data-label='Corrected to']")).to_contain_text("20.2")
        expect(row.locator("td[data-label='Reason']")).to_contain_text("Spike")

    def test_restored_corrections_can_be_hidden(self, page: Any, adapter: FakeAdapter) -> None:
        from hr_models import Quality

        correction = adapter.apply_correction(
            entity_id=ENTITY,
            state_id=2,
            expected_original="-2000.0",
            new_value="20.2",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        adapter.restore_correction(correction.id, "pytest")

        _goto(page, "/audit")
        expect(page.locator("tbody tr")).to_have_count(1)
        page.check("#hide-restored")
        expect(page.locator("tbody tr", has_text=ENTITY)).to_have_count(0)

    def test_hide_restored_survives_a_round_trip_through_the_topbar_nav(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        from hr_models import Quality

        correction = adapter.apply_correction(
            entity_id=ENTITY,
            state_id=2,
            expected_original="-2000.0",
            new_value="20.2",
            quality=Quality.SPIKE,
            note=None,
            created_by="pytest",
        )
        adapter.restore_correction(correction.id, "pytest")

        _goto(page, "/audit")
        page.check("#hide-restored")
        expect(page.locator("tbody tr", has_text=ENTITY)).to_have_count(0)

        page.click(".topbar nav a:has-text('Entities')")
        page.wait_for_load_state("networkidle")
        page.click(".topbar nav a:has-text('Corrections')")
        page.wait_for_load_state("networkidle")

        expect(page.locator("#hide-restored")).to_be_checked()
        expect(page.locator("tbody tr", has_text=ENTITY)).to_have_count(0)


class TestOnboarding:
    def test_the_wizard_gates_on_backup_and_connection(self, fresh_app: Any) -> None:
        server = make_server("127.0.0.1", 0, fresh_app, handler_class=_QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page()

                page.goto(base, wait_until="networkidle")
                assert page.url.endswith("/onboarding")

                finish = page.locator("#finish")
                expect(finish).to_be_disabled()

                # The backup acknowledgement alone is not enough.
                page.check("#backup-ack")
                expect(finish).to_be_disabled()

                page.click("#test-connection")
                expect(page.locator("#connection-status")).to_contain_text("Connected")
                expect(finish).to_be_enabled()

                page.click("#finish")
                page.wait_for_load_state("networkidle")
                expect(page.locator("h1")).to_have_text("Entities")

                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)


class TestEmptyRange:
    def test_an_empty_range_explains_itself_instead_of_showing_a_blank_chart(
        self, page: Any, adapter: FakeAdapter
    ) -> None:
        # Real cause: a sensor that stopped reporting, or a recorder purge.
        # Found by the browser suite, which rendered a blank canvas with no
        # explanation and looked identical to a broken page.
        adapter.add_entity("sensor.silent", SensorType.MEASUREMENT)
        _goto(page, "/entity/sensor.silent")
        expect(page.locator("#empty-notice")).to_be_visible()
        expect(page.locator("#empty-notice")).to_contain_text("No readings in this range")


class TestEntityFilterRow:
    """The entities page's type dropdown and search box, and the gap between
    that row and the table below it.

    A `<select>` does not honour the padding/line-height combination that
    gives a text input its height — measured side-by-side, the dropdown came
    out 38px against the search box's 40.5px, a visible misalignment. The
    filter row also had no bottom margin at all: its bottom edge and the
    table's top edge landed on the exact same pixel, so the controls sat
    flush against the table with no breathing room.
    """

    def test_the_filter_and_search_box_are_the_same_height(self, page: Any) -> None:
        page.set_viewport_size({"width": 1024, "height": 900})
        _goto(page, "/")
        heights = page.evaluate("""() => ({
            select: document.getElementById('type-filter').getBoundingClientRect().height,
            search: document.getElementById('search').getBoundingClientRect().height,
        })""")
        assert heights["select"] == heights["search"]

    def test_the_filter_row_leaves_a_gap_above_the_table(self, page: Any) -> None:
        page.set_viewport_size({"width": 1024, "height": 900})
        _goto(page, "/")
        gap = page.evaluate("""() => {
            const spreadBottom = document.querySelector('.spread').getBoundingClientRect().bottom;
            const tableTop = document.querySelector('.table-scroll').getBoundingClientRect().top;
            return tableTop - spreadBottom;
        }""")
        assert gap >= 10


class TestAuditFilterRow:
    """The corrections page's "Hide restored" checkbox and Export CSV button
    sit in the same kind of row as the entities page's filter/search box, and
    share the same `.spread`/`.row` rules — so the entities page's gap fixes
    (margin below the row, spacing between controls) apply here too. Checked
    directly rather than assumed, since the two pages' markup differs enough
    (a checkbox+label pairing instead of a dropdown) that a shared class does
    not guarantee a shared result.
    """

    def test_the_row_leaves_a_gap_above_the_table(self, page: Any) -> None:
        page.set_viewport_size({"width": 1024, "height": 900})
        _goto(page, "/audit")
        gap = page.evaluate("""() => {
            const spreadBottom = document.querySelector('.spread').getBoundingClientRect().bottom;
            const tableTop = document.querySelector('.table-scroll').getBoundingClientRect().top;
            return tableTop - spreadBottom;
        }""")
        assert gap >= 10

    def test_the_checkbox_and_its_label_do_not_touch(self, page: Any) -> None:
        page.set_viewport_size({"width": 1024, "height": 900})
        _goto(page, "/audit")
        gap = page.evaluate("""() => {
            const checkbox = document.getElementById('hide-restored');
            const span = document.querySelector('.spread .row label span');
            return span.getBoundingClientRect().left - checkbox.getBoundingClientRect().right;
        }""")
        assert gap >= 6

    def test_the_checkbox_label_and_export_button_do_not_touch(self, page: Any) -> None:
        page.set_viewport_size({"width": 1024, "height": 900})
        _goto(page, "/audit")
        gap = page.evaluate("""() => {
            const label = document.querySelector('.spread .row label');
            const exportBtn = document.getElementById('export-csv');
            return exportBtn.getBoundingClientRect().left - label.getBoundingClientRect().right;
        }""")
        assert gap >= 10


class TestTableNeverScrolls:
    """The entity and audit tables must never become horizontally scrollable
    at a width where the full table layout (not the mobile stacked-card one)
    is in effect.

    A prior version of the right-hand gap fix shrank the table itself to
    `width: calc(100% - 18px)`, leaving its parent `.table-scroll` at the
    container's full width — two independently-computed numbers that only
    avoid overflowing each other by arithmetic coincidence. A real device was
    seen to render it scrolled anyway, and this suite reproduced it: at
    700px with a long entity_id present, a fixed-layout table's percentage
    column widths (which sum to exactly 100 on paper) rendered the table 2px
    wider than its container — a border-collapse rounding effect, not a
    coincidence of the calc(). `.table-scroll`'s own `scrollWidth` still
    reflects that 2px surplus even after the fix (browsers report a box's
    true content size regardless of whether overflow is visible), so the
    only assertion that actually distinguishes "looks fine" from "quietly
    scrollable" is whether the container can be scrolled at all — `overflow-x:
    hidden` (not `auto`) makes it so it never can, at the cost of silently
    clipping a rounding-error's worth of pixel, which `overflow-wrap: anywhere`
    guarantees is never a character a user needed to read. Checked here under
    both Chromium (`page`, the shared fixture) and WebKit (Safari's engine,
    closer to the iOS Home Assistant app that originally showed the bug) at a
    width above the 640px mobile breakpoint, with a long entity_id present.
    """

    @pytest.mark.parametrize("width", [700, 800, 1024])
    def test_entity_table_chromium(self, page: Any, adapter: FakeAdapter, width: int) -> None:
        # A real user cannot set scrollLeft directly, so the check drives a
        # wheel gesture over the table instead — the same input a trackpad
        # swipe or an iOS touch-drag would send, and the one a visible
        # scrollbar would previously have responded to.
        adapter.add_entity("sensor.a_fairly_long_but_realistic_entity_name_for_a_sensor")
        page.set_viewport_size({"width": width, "height": 900})
        _goto(page, "/")
        box = page.locator(".table-scroll").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.wheel(delta_x=300, delta_y=0)
        left_after_wheel = page.evaluate("() => document.querySelector('.table-scroll').scrollLeft")
        assert left_after_wheel == 0, (
            f"table-scroll is scrollable at {width}px (scrollLeft moved to {left_after_wheel})"
        )

    def test_entity_table_webkit(self, app: Any, adapter: FakeAdapter) -> None:
        adapter.add_entity("sensor.a_fairly_long_but_realistic_entity_name_for_a_sensor")
        server = make_server("127.0.0.1", 0, app, handler_class=_QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.webkit.launch()
                webkit_page = browser.new_page()
                webkit_page.set_viewport_size({"width": 700, "height": 900})
                webkit_page.goto(base, wait_until="networkidle")
                box = webkit_page.locator(".table-scroll").bounding_box()
                webkit_page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                webkit_page.mouse.wheel(delta_x=300, delta_y=0)
                left_after_wheel = webkit_page.evaluate(
                    "() => document.querySelector('.table-scroll').scrollLeft"
                )
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
        assert left_after_wheel == 0, (
            f"table-scroll is scrollable under WebKit (scrollLeft moved to {left_after_wheel})"
        )


class TestCascadeGateStartsDisabledUnderWebkit:
    def test_a_real_click_on_a_counter_point_starts_save_disabled_under_webkit(
        self, app: Any, adapter: FakeAdapter
    ) -> None:
        # Reported against real Safari: the Save button started enabled on
        # the very first popup for a counter point, only reacting correctly
        # to the checkbox from the first manual toggle onward — not
        # reproduced here even with a genuine mouse click routed through
        # Chart.js's own hit-testing (rather than calling onClick directly)
        # under Playwright's WebKit build, so the exact mechanism stays
        # unconfirmed. A `requestAnimationFrame` re-assertion was tried as a
        # defensive fix and made things worse (it broke the checkbox's own
        # toggle reacting at all, reported directly, reverted without ever
        # being root-caused) — kept here only as coverage for what actually
        # shipped: `<button id="p-save" disabled>`'s static HTML default
        # (entity.html), so a JS failure/race fails safe rather than
        # silently permissive.
        adapter.add_entity("sensor.energy_total", SensorType.COUNTER)
        adapter.add_statistics_metadata("sensor.energy_total", mean_type=0, has_sum=True)
        adapter.add_state("sensor.energy_total", 9, SEED_TIMESTAMPS[0], "999999.0")
        server = make_server("127.0.0.1", 0, app, handler_class=_QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.webkit.launch()
                webkit_page = browser.new_page()
                webkit_page.goto(f"{base}/entity/sensor.energy_total", wait_until="networkidle")
                # Chart.js's default entrance animation moves each point from
                # y=0 up into place over ~1000ms — reading its pixel position
                # before that settles gets an intermediate, still-animating
                # coordinate rather than the final one, and the click below
                # misses the real point. Not visible locally on a fast,
                # otherwise-idle machine, but reliably reproduced on a slower,
                # shared CI runner.
                webkit_page.wait_for_timeout(1500)
                point = webkit_page.evaluate(
                    """() => {
                        const chart = Chart.getChart(document.getElementById('chart'));
                        const index = chart.data.datasets[0].data.findIndex((p) => p.y === 999999.0);
                        if (index < 0) return null;
                        const meta = chart.getDatasetMeta(0).data[index];
                        const rect = chart.canvas.getBoundingClientRect();
                        return { x: rect.left + meta.x, y: rect.top + meta.y };
                    }"""
                )
                assert point is not None
                webkit_page.mouse.click(point["x"], point["y"])
                webkit_page.wait_for_timeout(200)
                assert webkit_page.eval_on_selector("#panel", "el => el.hidden") is False
                assert webkit_page.eval_on_selector("#p-save", "el => el.disabled") is True
                assert (
                    webkit_page.eval_on_selector("#p-cascade-ack-row", "el => el.hidden") is False
                )
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
