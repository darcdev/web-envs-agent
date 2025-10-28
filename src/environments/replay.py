import asyncio
import logging
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse
from playwright.async_api import Page, Locator, TimeoutError as PlaywrightTimeoutError
from db.models import StepModel


logger = logging.getLogger(__name__)
# TODO: potential fixes to debug if failing
# - is the CSS scaping enough given our usecase?
# - is URLs differ to simple given the purpose of this? which is replicating a set of human trajectory steps from the DB
# - are wait and page timeouts set properly?
# - are we handling scroll offsets properly?


class TaskStepExecutor:
    def __init__(
        self, trajectory: list[StepModel], *, run_human_trajectory: bool = False
    ) -> None:
        self.trajectory: list[StepModel] = trajectory
        self.run_human_trajectory: bool = run_human_trajectory
        self._initial_navigation_done: bool = False

    async def run(self, page: Page) -> None:
        if page.url and page.url != "about:blank":
            self._initial_navigation_done = True

        for step in self.trajectory:
            try:
                await self._run_step(page, step)
            except Exception as exc:
                logger.error(
                    "Failed to execute step %s (%s): %s. Stopping trajectory replay.",
                    step.id,
                    step.event_type,
                    exc,
                    exc_info=True,
                )
                return
            base_delay: float = 0.2 if self.run_human_trajectory else 0.1
            await asyncio.sleep(base_delay)

    async def _run_step(self, page: Page, step: StepModel) -> None:
        category, subject, action = self._split_event_type(step.event_type)

        if category == "state":
            await self._handle_state_step(page, subject, action, step.event_data_json)
            return

        if category == "action" and subject == "user":
            await self._handle_user_action(page, action, step.event_data_json)

    async def _handle_state_step(
        self, page: Page, subject: str, action: str, payload: Dict[str, Any]
    ) -> None:
        if subject == "browser" and action == "navigated":
            url = payload.get("url") if isinstance(payload, dict) else None
            if not url or url == "about:blank":
                return
            if not self._initial_navigation_done or self._urls_differ(page.url, url):
                # Check if this is a SPA route change vs real navigation
                if await self._is_spa_route_change(page, url):
                    await self._perform_spa_navigation(page, url)
                else:
                    await self._safe_goto(page, url)
                self._initial_navigation_done = True
            return

        if subject == "page":
            if action in {"domcontentloaded", "domcontentload"}:
                await self._safe_wait_for_load(page, "domcontentloaded")
            elif action in {"loaded", "load"}:
                await self._safe_wait_for_load(page, "load")

    async def _handle_user_action(
        self, page: Page, action: str, payload: Dict[str, Any]
    ) -> None:
        if action == "click":
            await self._perform_pointer_click(page, payload)
            return
        if action == "hover":
            await self._perform_pointer_move(page, payload)
            return
        if action == "scroll":
            await self._perform_scroll(page, payload)
            return
        if action == "input":
            await self._perform_input(page, payload)
            return
        if action == "keydown":
            await self._perform_keydown(page, payload)
            return
        if action == "submit":
            await self._perform_submit(page, payload)

    async def _perform_pointer_click(self, page: Page, payload: Dict[str, Any]) -> None:
        coords: Optional[Tuple[float, float]] = self._extract_coordinates(payload)
        if coords is None:
            selector: Optional[str] = self._build_selector(payload)
            if selector:
                try:
                    # Use locator for better reliability and auto-waiting
                    await page.locator(selector).click(timeout=5000)
                except PlaywrightTimeoutError:
                    logger.error(
                        "Failed to click element with selector: %s",
                        selector,
                        exc_info=True,
                    )
                    raise
            else:
                logger.error(
                    "Cannot perform click: no coordinates or selector available"
                )
                raise Exception("No coordinates or selector available for click")
            return
        x, y = coords
        await page.mouse.move(x, y)
        await asyncio.sleep(0.1)
        await page.mouse.click(x, y)

    async def _perform_pointer_move(self, page: Page, payload: Dict[str, Any]) -> None:
        coords: Optional[Tuple[float, float]] = self._extract_coordinates(payload)
        if coords is None:
            return
        x, y = coords
        await page.mouse.move(x, y)

    async def _perform_scroll(self, page: Page, payload: Dict[str, Any]) -> None:
        x: Any = payload.get("x")
        y: Any = payload.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            try:
                # Use evaluate for absolute scroll positioning (acceptable use case per Playwright docs)
                # as there's no direct API for setting exact scroll coordinates
                await page.evaluate(
                    """(coords) => {
                        // Try multiple methods to ensure scroll happens
                        window.scrollTo({
                            left: coords.x,
                            top: coords.y,
                            behavior: 'instant'
                        });
                        // Fallback for older browsers
                        if (window.scrollX !== coords.x || window.scrollY !== coords.y) {
                            window.scrollTo(coords.x, coords.y);
                        }
                        // Also try scrolling document element directly
                        if (document.documentElement) {
                            document.documentElement.scrollLeft = coords.x;
                            document.documentElement.scrollTop = coords.y;
                        }
                    }""",
                    {"x": x, "y": y},
                )
            except Exception as exc:
                logger.warning("Failed to scroll to (%s, %s): %s", x, y, exc)
        else:
            logger.warning("Invalid scroll coordinates: x=%s, y=%s", x, y)

    async def _perform_input(self, page: Page, payload: Dict[str, Any]) -> None:
        value: Optional[str] = (
            payload.get("value") if isinstance(payload, dict) else None
        )
        if value is None:
            return

        # Try to use locator with selector first
        selector: Optional[str] = self._build_selector(payload)
        if selector:
            try:
                # Use locator.fill() for better reliability and auto-waiting
                locator: Locator = page.locator(selector)
                await locator.fill(value, timeout=5000)
                return
            except PlaywrightTimeoutError:
                logger.error("Failed to fill element with selector: %s", selector)

        # Fallback to focused element
        try:
            focused_locator: Locator = page.locator(":focus")
            if await focused_locator.count() > 0:
                await focused_locator.fill(value, timeout=2000)
                return
        except Exception as exc:
            logger.error("Failed to fill focused element: %s", exc)

        # If we get here, both attempts failed
        logger.error(
            "Input failed: could not fill value '%s' using selector '%s' or focused element",
            value,
            selector,
        )
        raise Exception("Input operation failed")

    async def _perform_keydown(self, page: Page, payload: Dict[str, Any]) -> None:
        key: Optional[str] = payload.get("key") if isinstance(payload, dict) else None
        if not key:
            return
        try:
            await page.keyboard.press(key)
        except Exception:
            await page.keyboard.type(key)

    async def _perform_submit(self, page: Page, payload: Dict[str, Any]) -> None:
        # Try to find and submit the form using locators
        selector: Optional[str] = self._build_selector(payload)

        try:
            if selector:
                # Try pressing Enter on the form element
                form_locator: Locator = page.locator(selector)
                if await form_locator.count() > 0:
                    await form_locator.press("Enter", timeout=2000)
                    return

            # Try to find and click a submit button
            submit_button: Locator = page.locator(
                'button[type="submit"], input[type="submit"]'
            ).first
            if await submit_button.count() > 0:
                await submit_button.click(timeout=2000)
                return

            # Fallback: press Enter on the focused element or first form
            focused: Locator = page.locator(":focus")
            if await focused.count() > 0:
                await focused.press("Enter", timeout=2000)
                return

            # Last resort: press Enter on the first form
            first_form: Locator = page.locator("form").first
            if await first_form.count() > 0:
                await first_form.press("Enter", timeout=2000)
                return

            # If we reach here, no submit method worked
            logger.error("Submit failed: no form element found to submit")
            raise Exception("Submit operation failed")
        except Exception as exc:
            logger.error("Failed to submit form: %s", exc, exc_info=True)
            raise

    async def _is_spa_route_change(self, page: Page, target_url: str) -> bool:
        """
        Detect if a navigation is likely a SPA client-side route change.

        A navigation is considered a SPA route change if:
        1. Same origin as current page
        2. Only hash differs, OR
        3. Path/query differs but it's on the same domain (likely client-side routing)
        """
        current_url = page.url
        if not current_url or current_url == "about:blank":
            return False

        try:
            current = urlparse(current_url)
            target = urlparse(target_url)

            # Different origins = real navigation
            if current.scheme != target.scheme or current.netloc != target.netloc:
                return False

            # Same origin navigation
            # If only hash differs, it's definitely a SPA route change
            if (
                current.scheme == target.scheme
                and current.netloc == target.netloc
                and current.path == target.path
                and current.query == target.query
            ):
                return True  # Only hash differs

            # For same-origin path changes, assume it's SPA if:
            # - The path doesn't end with .html, .htm, .php, etc.
            # - The current page is already loaded (not the initial navigation)
            static_extensions = {".html", ".htm", ".php", ".asp", ".jsp"}
            target_path = target.path.lower()

            if any(target_path.endswith(ext) for ext in static_extensions):
                return False  # Likely a real document

            # Assume same-origin path change without static extension is SPA
            return True

        except Exception as exc:
            logger.warning("Error checking SPA route change: %s", exc)
            return False

    async def _perform_spa_navigation(self, page: Page, url: str) -> None:
        """
        Perform client-side navigation for SPA routes using History API.
        This avoids triggering a full page load.
        """
        try:
            logger.info("SPA navigation to %s (using History API)", url)

            # Use History API to change URL without reloading
            await page.evaluate(
                """(url) => {
                window.history.pushState({}, '', url);
                // Trigger popstate event in case the SPA listens to it
                window.dispatchEvent(new PopStateEvent('popstate', { state: {} }));
                // Also trigger hashchange if hash changed
                window.dispatchEvent(new HashChangeEvent('hashchange'));
            }""",
                url,
            )

            # Give the SPA time to react to the route change
            await asyncio.sleep(0.3)

        except Exception as exc:
            logger.warning(
                "Failed SPA navigation to %s: %s, falling back to goto", url, exc
            )
            # Fallback to regular navigation if SPA navigation fails
            await self._safe_goto(page, url)

    async def _safe_goto(self, page: Page, url: str) -> None:
        try:
            logger.info("Navigating to %s", url)
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            logger.warning("Failed to navigate to %s: %s", url, exc)

    async def _safe_wait_for_load(self, page: Page, state: str) -> None:
        try:
            await page.wait_for_load_state(state, timeout=15000)  # type: ignore
        except Exception as exc:
            logger.debug("Load wait for %s skipped: %s", state, exc)

    def _extract_coordinates(
        self, payload: Dict[str, Any]
    ) -> Optional[Tuple[float, float]]:
        coords: Any = payload.get("coordinates") if isinstance(payload, dict) else None
        if isinstance(coords, dict):
            for key in ("client", "page", "offset"):
                point: Any = coords.get(key)
                if self._is_valid_point(point):
                    return float(point["x"]), float(point["y"])
            relative: Any = coords.get("relative")
            viewport: Any = coords.get("viewport") or payload.get("viewport")
            if (
                self._is_valid_point(relative)
                and isinstance(viewport, dict)
                and isinstance(viewport.get("width"), (int, float))
                and isinstance(viewport.get("height"), (int, float))
            ):
                return (
                    float(relative["x"]) * float(viewport["width"]),
                    float(relative["y"]) * float(viewport["height"]),
                )
        x: Any = payload.get("x") if isinstance(payload, dict) else None
        y: Any = payload.get("y") if isinstance(payload, dict) else None
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            return float(x), float(y)
        rect: Any = payload.get("elementRect") if isinstance(payload, dict) else None
        if isinstance(rect, dict):
            left: Any = rect.get("left")
            top: Any = rect.get("top")
            width: Any = rect.get("width", 0)
            height: Any = rect.get("height", 0)
            if isinstance(left, (int, float)) and isinstance(top, (int, float)):
                return float(left + width / 2), float(top + height / 2)
        return None

    def _build_selector(self, payload: Dict[str, Any]) -> Optional[str]:
        element_id: Any = payload.get("id") if isinstance(payload, dict) else None
        if element_id:
            return f"#{self._css_escape(str(element_id))}"
        class_name: Any = (
            payload.get("className") if isinstance(payload, dict) else None
        )
        tag: Any = payload.get("tag") if isinstance(payload, dict) else None
        if class_name:
            classes: list[str] = [
                self._css_escape(part) for part in str(class_name).split() if part
            ]
            if classes:
                prefix: str = (tag or "*").lower() if tag else "*"
                return f"{prefix}{''.join('.' + cls for cls in classes)}"
        return None

    @staticmethod
    def _is_valid_point(point: Any) -> bool:
        return (
            isinstance(point, dict)
            and isinstance(point.get("x"), (int, float))
            and isinstance(point.get("y"), (int, float))
        )

    @staticmethod
    def _css_escape(value: str) -> str:
        return "".join(CSS_ESCAPE_MAP.get(ch, ch) for ch in value)

    @staticmethod
    def _split_event_type(event_type: str) -> Tuple[str, str, str]:
        parts = (event_type or "").split(":", 2)
        if len(parts) == 1:
            return parts[0], "", ""
        if len(parts) == 2:
            return parts[0], parts[1], ""
        return parts[0], parts[1], parts[2]

    @staticmethod
    def _urls_differ(current: Optional[str], target: str) -> bool:
        if not current:
            return True
        return current.rstrip("/") != target.rstrip("/")


# Escape characters for CSS selectors
CSS_ESCAPE_MAP = {
    "\n": "\\A ",
    "\r": "",
    "\f": "\\C ",
    "\t": " ",
    " ": " ",
    '"': '\\"',
    "'": "\\'",
    "#": "\\#",
    ":": "\\:",
}
