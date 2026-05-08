from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

wikiGachaUrl = "https://wikigacha.com/?lang=ZH_HANT"


def parseArguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Conservative, persistent-profile browser automation for Wikipedia Gacha. "
            "It keeps the same browser profile across runs so local storage, cookies, "
            "Google sign-in, and optional server sync state can be reused."
        )
    )
    parser.add_argument(
        "--drawCount",
        type=int,
        default=1,
        help="Number of draw actions to perform. This is the only intentional run-length control.",
    )
    parser.add_argument(
        "--profileDir",
        default=".wikigacha-profile",
        help="Persistent browser profile directory. Reuse the same value to keep the same account/session.",
    )
    parser.add_argument(
        "--evidenceDir",
        default="wikigacha_results",
        help="Directory for screenshots and non-secret state reports.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser. Recommended for the first run and Google sign-in / server-sync setup.",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Open the site with the persistent profile and pause for manual Google sign-in / server-sync setup.",
    )
    parser.add_argument(
        "--url",
        default=wikiGachaUrl,
        help="Target URL. Keep the default unless the service changes its language route.",
    )
    parser.add_argument(
        "--locale",
        default="zh-TW",
        help="Browser locale used by the persistent context.",
    )
    parser.add_argument(
        "--dryRun",
        action="store_true",
        help="Resolve the draw target and save evidence without clicking it.",
    )
    return parser.parse_args()


def ensureArgumentsAreValid(arguments: argparse.Namespace) -> None:
    if arguments.drawCount < 1:
        raise ValueError("--drawCount 必須大於 0。")


def createEvidenceDirectory(evidenceDir: str) -> Path:
    evidencePath = Path(evidenceDir)
    evidencePath.mkdir(parents=True, exist_ok=True)
    return evidencePath


def buildShortHash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:16]


def getPageFingerprint(page: Page) -> str:
    return page.evaluate(
        """
        () => {
            const storageEntries = {};
            for (let index = 0; index < localStorage.length; index += 1) {
                const key = localStorage.key(index);
                storageEntries[key] = localStorage.getItem(key);
            }
            return JSON.stringify({
                href: location.href,
                title: document.title,
                visibleText: document.body ? document.body.innerText : "",
                storageEntries,
            });
        }
        """
    )


def waitForPageReady(page: Page) -> None:
    page.wait_for_load_state("domcontentloaded", timeout=0)
    try:
        page.wait_for_load_state("networkidle", timeout=0)
    except PlaywrightTimeoutError:
        # timeout=0 disables Playwright's deadline; this branch is defensive for non-standard runtimes.
        pass
    page.wait_for_function(
        """
        () => document.readyState === "complete" || document.readyState === "interactive"
        """,
        timeout=0,
    )


def dismissObstructiveUi(page: Page) -> None:
    page.keyboard.press("Escape")
    page.evaluate(
        """
        () => {
            const dialogCandidates = Array.from(document.querySelectorAll('[role="dialog"], dialog'));
            for (const dialogCandidate of dialogCandidates) {
                const closeCandidate = Array.from(dialogCandidate.querySelectorAll('button, [role="button"], a'))
                    .find((element) => {
                        const text = [
                            element.innerText,
                            element.getAttribute('aria-label'),
                            element.getAttribute('title')
                        ].filter(Boolean).join(' ').toLowerCase();
                        return /^(×|x|ok|close|dismiss|確定|關閉|同意|閉じる)$/iu.test(text.trim());
                    });
                if (closeCandidate instanceof HTMLElement) {
                    closeCandidate.click();
                }
            }
        }
        """
    )


def resolveDrawTargetSelector(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """
        () => {
            const positivePatterns = [
                /gacha/iu,
                /pack/iu,
                /open/iu,
                /draw/iu,
                /pull/iu,
                /ガチャ/iu,
                /パック/iu,
                /開封/iu,
                /開く/iu,
                /引く/iu,
                /回す/iu,
                /抽/iu,
                /抽卡/iu,
                /召喚/iu,
                /開包/iu,
                /開啟/iu
            ];
            const negativePatterns = [
                /privacy/iu,
                /policy/iu,
                /terms/iu,
                /contact/iu,
                /wikipedia\.org/iu,
                /圖鑑/iu,
                /図鑑/iu,
                /battle/iu,
                /バトル/iu,
                /隱私/iu,
                /條款/iu,
                /お問い合わせ/iu
            ];
            const clickableSelector = [
                'button',
                'a',
                'input[type="button"]',
                'input[type="submit"]',
                '[role="button"]',
                '[onclick]',
                '[tabindex]'
            ].join(',');
            const elements = Array.from(document.querySelectorAll(clickableSelector));
            const viewportArea = Math.max(1, window.innerWidth * window.innerHeight);
            const candidates = elements
                .filter((element) => element instanceof HTMLElement)
                .filter((element) => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && rect.width > 0
                        && rect.height > 0
                        && !element.hasAttribute('disabled')
                        && element.getAttribute('aria-disabled') !== 'true';
                })
                .map((element, index) => {
                    const rect = element.getBoundingClientRect();
                    const textParts = [
                        element.innerText,
                        element.textContent,
                        element.getAttribute('aria-label'),
                        element.getAttribute('title'),
                        element.getAttribute('value'),
                        element.getAttribute('href'),
                        element.id,
                        element.className,
                        element.getAttribute('data-testid')
                    ].filter(Boolean).join(' ');
                    const positiveScore = positivePatterns.reduce(
                        (score, pattern) => score + (pattern.test(textParts) ? 1 : 0),
                        0
                    );
                    const negativeScore = negativePatterns.reduce(
                        (score, pattern) => score + (pattern.test(textParts) ? 1 : 0),
                        0
                    );
                    const roleScore = /button/i.test(element.tagName) || element.getAttribute('role') === 'button' ? 1 : 0;
                    const areaScore = Math.log1p((rect.width * rect.height) / viewportArea);
                    const centerBias = 1 - Math.min(
                        1,
                        Math.hypot(
                            (rect.left + rect.width / 2 - window.innerWidth / 2) / Math.max(1, window.innerWidth),
                            (rect.top + rect.height / 2 - window.innerHeight / 2) / Math.max(1, window.innerHeight)
                        )
                    );
                    const semanticScore = positiveScore - negativeScore;
                    const score = semanticScore * 10 + roleScore + areaScore + centerBias;
                    const marker = `auto-wikigacha-target-${Date.now()}-${index}`;
                    return {
                        element,
                        marker,
                        score,
                        text: textParts.replace(/\s+/g, ' ').trim().slice(0, 240),
                        tagName: element.tagName.toLowerCase(),
                        role: element.getAttribute('role') || '',
                        href: element.getAttribute('href') || '',
                        positiveScore,
                        negativeScore,
                        roleScore,
                        areaScore,
                        centerBias
                    };
                })
                .sort((left, right) => right.score - left.score);

            if (candidates.length === 0) {
                return {
                    ok: false,
                    reason: 'No visible clickable candidate was found.',
                    candidates: []
                };
            }

            const selectedCandidate = candidates[0];
            selectedCandidate.element.setAttribute('data-auto-wikigacha-target', selectedCandidate.marker);
            return {
                ok: true,
                selector: `[data-auto-wikigacha-target="${selectedCandidate.marker}"]`,
                selected: {
                    score: selectedCandidate.score,
                    text: selectedCandidate.text,
                    tagName: selectedCandidate.tagName,
                    role: selectedCandidate.role,
                    href: selectedCandidate.href,
                    positiveScore: selectedCandidate.positiveScore,
                    negativeScore: selectedCandidate.negativeScore,
                    roleScore: selectedCandidate.roleScore,
                    areaScore: selectedCandidate.areaScore,
                    centerBias: selectedCandidate.centerBias
                },
                candidates: candidates.slice(0, 12).map((candidate) => ({
                    score: candidate.score,
                    text: candidate.text,
                    tagName: candidate.tagName,
                    role: candidate.role,
                    href: candidate.href,
                    positiveScore: candidate.positiveScore,
                    negativeScore: candidate.negativeScore,
                    roleScore: candidate.roleScore,
                    areaScore: candidate.areaScore,
                    centerBias: candidate.centerBias
                }))
            };
        }
        """
    )


def waitForDrawResult(page: Page, previousFingerprint: str) -> None:
    page.wait_for_function(
        """
        (previousFingerprintValue) => {
            const storageEntries = {};
            for (let index = 0; index < localStorage.length; index += 1) {
                const key = localStorage.key(index);
                storageEntries[key] = localStorage.getItem(key);
            }
            const currentFingerprintValue = JSON.stringify({
                href: location.href,
                title: document.title,
                visibleText: document.body ? document.body.innerText : "",
                storageEntries,
            });
            return currentFingerprintValue !== previousFingerprintValue;
        }
        """,
        arg=previousFingerprint,
        timeout=0,
    )
    try:
        page.wait_for_load_state("networkidle", timeout=0)
    except PlaywrightTimeoutError:
        pass


def writeJson(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def collectNonSecretStorageSummary(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """
        () => {
            const localStorageKeys = [];
            const localStorageValueHashes = {};
            for (let index = 0; index < localStorage.length; index += 1) {
                const key = localStorage.key(index);
                const value = localStorage.getItem(key) || '';
                localStorageKeys.push(key);
                localStorageValueHashes[key] = value.length;
            }
            return {
                href: location.href,
                title: document.title,
                localStorageKeys,
                localStorageValueLengths: localStorageValueHashes,
                userAgent: navigator.userAgent,
                language: navigator.language,
                languages: navigator.languages
            };
        }
        """
    )


def saveEvidence(page: Page, evidencePath: Path, label: str, extraPayload: dict[str, Any]) -> None:
    timestampText = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    screenshotPath = evidencePath / f"{timestampText}_{label}.png"
    reportPath = evidencePath / f"{timestampText}_{label}.json"
    page.screenshot(path=str(screenshotPath), full_page=True)
    storageSummary = collectNonSecretStorageSummary(page)
    payload = {
        "createdAtUtc": timestampText,
        "label": label,
        "url": page.url,
        "storageSummary": storageSummary,
        "extra": extraPayload,
    }
    writeJson(reportPath, payload)
    print(f"[INFO] Saved screenshot: {screenshotPath}")
    print(f"[INFO] Saved non-secret report: {reportPath}")


def runSetup(page: Page, targetUrl: str, evidencePath: Path) -> None:
    page.goto(targetUrl, wait_until="domcontentloaded", timeout=0)
    waitForPageReady(page)
    print("[SETUP] 請在開啟的瀏覽器中完成 Google 登入，並於站內手動啟用 server sync。")
    print("[SETUP] 完成後回到終端機按 Enter；這個持久化 profile 會在後續執行中沿用。")
    input()
    saveEvidence(page, evidencePath, "setup_complete", {"mode": "setup"})


def performDraws(page: Page, arguments: argparse.Namespace, evidencePath: Path) -> None:
    page.goto(arguments.url, wait_until="domcontentloaded", timeout=0)
    waitForPageReady(page)
    dismissObstructiveUi(page)
    saveEvidence(page, evidencePath, "before_draw", {"drawCount": arguments.drawCount})

    for drawIndex in range(1, arguments.drawCount + 1):
        print(f"[INFO] Resolving draw target for draw {drawIndex}/{arguments.drawCount}")
        targetResolution = resolveDrawTargetSelector(page)
        if not targetResolution.get("ok"):
            saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_no_target", targetResolution)
            raise RuntimeError(targetResolution.get("reason", "No draw target was found."))

        saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_target", targetResolution)
        if arguments.dryRun:
            print("[DRY-RUN] Target resolved; click skipped.")
            continue

        previousFingerprint = getPageFingerprint(page)
        locator = page.locator(targetResolution["selector"])
        locator.click(timeout=0)
        waitForDrawResult(page, previousFingerprint)
        currentFingerprint = getPageFingerprint(page)
        saveEvidence(
            page,
            evidencePath,
            f"draw_{drawIndex:03d}_result",
            {
                "targetResolution": targetResolution,
                "previousFingerprintHash": buildShortHash(previousFingerprint),
                "currentFingerprintHash": buildShortHash(currentFingerprint),
            },
        )


def main() -> int:
    arguments = parseArguments()
    ensureArgumentsAreValid(arguments)
    evidencePath = createEvidenceDirectory(arguments.evidenceDir)
    profilePath = Path(arguments.profileDir)
    profilePath.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profilePath),
            headless=not arguments.headed,
            locale=arguments.locale,
            viewport={"width": 1280, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            if arguments.setup:
                runSetup(page, arguments.url, evidencePath)
            else:
                performDraws(page, arguments, evidencePath)
        finally:
            context.close()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user.", file=sys.stderr)
        raise SystemExit(130)
