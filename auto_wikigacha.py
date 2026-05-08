from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

wikiGachaUrl = "https://wikigacha.com/?lang=ZH_HANT"


class WikiGachaAutomationError(RuntimeError):
    pass


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
        help="Compatibility flag. The browser is visible by default; use --headless to hide it.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without showing a browser window. Omit this flag to watch the real Chrome/Chromium actions.",
    )
    parser.add_argument(
        "--browserChannel",
        default="chrome",
        help=(
            "Preferred browser channel. The default tries installed Google Chrome first and falls back "
            "to Playwright Chromium if Chrome is unavailable. Use an empty string to skip channel selection."
        ),
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
        help="Resolve the entry-gate and draw targets, save evidence, but do not click the draw target.",
    )
    parser.add_argument(
        "--keepEntryNotices",
        action="store_true",
        help="Do not tick 'do not show again' style checkboxes on entry/update notices.",
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
        r"""
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
    page.wait_for_function(
        r"""
        () => document.body && (document.readyState === "complete" || document.readyState === "interactive")
        """,
        timeout=0,
    )
    waitForRenderCycle(page)


def waitForRenderCycle(page: Page) -> dict[str, Any]:
    return page.evaluate(
        r"""
        () => new Promise((resolve) => {
            let mutationCount = 0;
            const observer = new MutationObserver((mutations) => {
                mutationCount += mutations.length;
            });
            if (document.documentElement) {
                observer.observe(document.documentElement, {
                    attributes: true,
                    childList: true,
                    characterData: true,
                    subtree: true,
                });
            }
            requestAnimationFrame(() => {
                requestAnimationFrame(() => {
                    observer.disconnect();
                    resolve({ mutationCount });
                });
            });
        })
        """
    )


def resolveEntryGateActionSelector(page: Page) -> dict[str, Any]:
    return page.evaluate(
        r"""
        () => {
            const markerPrefix = `auto-wikigacha-entry-${Date.now()}-${Math.random().toString(36).slice(2)}`;
            const clickableSelector = [
                'button',
                'a[href]',
                'input[type="button"]',
                'input[type="submit"]',
                '[role="button"]',
                '[onclick]',
                '[tabindex]'
            ].join(',');
            const confirmationPatterns = [
                /^(ok|okay|confirm|confirmed|continue|close|dismiss|enter|start|got it)$/iu,
                /^(確定|確認|關閉|關掉|知道了|我知道了|同意|繼續|進入|開始)$/iu,
                /^(确定|确认|关闭|知道了|我知道了|同意|继续|进入|开始)$/iu,
                /^(閉じる|確認|同意|開始|続ける|入る|了解)$/iu,
            ];
            const confirmationTokenPatterns = [
                /(^|[\s　])(?:ok|okay|confirm|confirmed|continue|close|dismiss|enter|start|got it)(?=$|[\s　])/iu,
                /(^|[\s　])(?:確定|確認|關閉|關掉|知道了|我知道了|同意|繼續|進入|開始)(?=$|[\s　])/iu,
                /(^|[\s　])(?:确定|确认|关闭|知道了|我知道了|同意|继续|进入|开始)(?=$|[\s　])/iu,
                /(^|[\s　])(?:閉じる|確認|同意|開始|続ける|入る|了解)(?=$|[\s　])/iu,
            ];
            const rootEvidencePatterns = [
                /更新通知|公告|通知|入口|歡迎|欢迎|說明|说明/iu,
                /notice|announcement|update|welcome|entry|modal|dialog/iu,
                /お知らせ|更新|通知|案内/iu,
            ];
            const negativePatterns = [
                /activity|campaign|event|details/iu,
                /活動詳情|活动详情|活動詳細|イベント詳細/iu,
                /server|sync|cloud|beta/iu,
                /伺服器同步|服务器同步|サーバー同期/iu,
                /language|語言|语言|言語/iu,
                /privacy|policy|terms|contact/iu,
                /隱私|隐私|條款|条款|聯絡|联系/iu,
                /圖鑑|图鉴|對戰|对战|獎盃|奖杯/iu
            ];
            const negativeInteractivePatterns = [
                /@harusugi5|x\.com|twitter\.com/iu,
                /activity|campaign|event|details/iu,
                /活動詳情|活动详情|活動詳細|イベント詳細/iu,
                /server|sync|cloud|beta/iu,
                /伺服器同步|服务器同步|サーバー同期/iu,
                /language|語言|语言|言語/iu,
                /privacy|policy|terms|contact/iu,
                /隱私|隐私|條款|条款|聯絡|联系/iu,
                /圖鑑|图鉴|對戰|对战|獎盃|奖杯/iu,
            ];
            const positiveDontShowPatterns = [
                /下次不再顯示|下次不再显示|don't show again|do not show again|次回から表示しない/iu
            ];

            const getClassText = (element) => typeof element.className === 'string' ? element.className : '';

            const normalizeWhitespace = (value) => String(value || '').replace(/\s+/g, ' ').trim();

            const getDisplayText = (element) => {
                if (!element) {
                    return '';
                }
                return normalizeWhitespace([
                    element.innerText,
                    element.textContent,
                    element.getAttribute ? element.getAttribute('aria-label') : '',
                    element.getAttribute ? element.getAttribute('title') : '',
                    element.getAttribute ? element.getAttribute('value') : '',
                    ...Array.from(element.querySelectorAll ? element.querySelectorAll('img[alt]') : [])
                        .map((image) => image.getAttribute('alt'))
                ].filter(Boolean).join(' '));
            };

            const getNormalizedText = (element) => {
                if (!element) {
                    return '';
                }
                return normalizeWhitespace([
                    getDisplayText(element),
                    element.getAttribute ? element.getAttribute('href') : '',
                    element.id,
                    getClassText(element),
                ].filter(Boolean).join(' '));
            };

            const isVisible = (element) => {
                if (!(element instanceof HTMLElement)) {
                    return false;
                }
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && rect.width > 0
                    && rect.height > 0
                    && !element.hasAttribute('disabled')
                    && element.getAttribute('aria-disabled') !== 'true';
            };

            const isPointerReceivable = (element) => {
                const rect = element.getBoundingClientRect();
                const centerX = rect.left + rect.width / 2;
                const centerY = rect.top + rect.height / 2;
                const hitElement = document.elementFromPoint(centerX, centerY);
                return Boolean(hitElement && (element === hitElement || element.contains(hitElement)));
            };

            const isLayerElement = (element) => {
                if (!(element instanceof HTMLElement)) {
                    return false;
                }
                const style = window.getComputedStyle(element);
                const role = element.getAttribute('role') || '';
                const classText = getClassText(element);
                const rect = element.getBoundingClientRect();
                const classIndicatesGateLayer = /(^|\s)(fixed|modal|dialog|overlay)(\s|$)/iu.test(classText);
                const coversViewport = rect.left <= 0
                    && rect.top <= 0
                    && rect.right >= window.innerWidth
                    && rect.bottom >= window.innerHeight;
                return element.tagName.toLowerCase() === 'dialog'
                    || role === 'dialog'
                    || role === 'alertdialog'
                    || element.getAttribute('aria-modal') === 'true'
                    || style.position === 'fixed'
                    || (coversViewport && style.zIndex !== 'auto')
                    || classIndicatesGateLayer;
            };

            const collectLayerChain = (element) => {
                const layers = [];
                let current = element;
                while (current && current instanceof HTMLElement) {
                    if (isLayerElement(current)) {
                        const style = window.getComputedStyle(current);
                        layers.push({
                            tagName: current.tagName.toLowerCase(),
                            role: current.getAttribute('role') || '',
                            position: style.position,
                            zIndex: style.zIndex,
                            text: getDisplayText(current).slice(0, 260),
                        });
                    }
                    current = current.parentElement;
                }
                return layers;
            };

            const findLayerRoot = (element) => {
                let current = element;
                let layerRoot = null;
                while (current && current instanceof HTMLElement) {
                    if (isLayerElement(current)) {
                        layerRoot = current;
                    }
                    current = current.parentElement;
                }
                return layerRoot;
            };

            const countVisibleActions = (root) => Array.from(root.querySelectorAll(clickableSelector))
                .filter(isVisible)
                .filter((element) => !element.matches('input[type="checkbox"], [role="checkbox"]'));

            const getEvidenceCount = (patterns, text) => patterns
                .map((pattern) => pattern.test(text))
                .filter(Boolean).length;

            const findDontShowControl = (element) => {
                const searchRoot = findLayerRoot(element) || document.body;
                const controls = Array.from(searchRoot.querySelectorAll('label, input[type="checkbox"], [role="checkbox"]'));
                for (const control of controls) {
                    const text = getDisplayText(control.parentElement || control);
                    if (positiveDontShowPatterns.some((pattern) => pattern.test(text))) {
                        const checkbox = control.matches('input[type="checkbox"], [role="checkbox"]')
                            ? control
                            : control.querySelector('input[type="checkbox"], [role="checkbox"]');
                        if (checkbox instanceof HTMLElement && isVisible(checkbox)) {
                            const marker = `${markerPrefix}-dont-show`;
                            checkbox.setAttribute('data-auto-wikigacha-entry', marker);
                            return {
                                selector: `[data-auto-wikigacha-entry="${marker}"]`,
                                text: text.slice(0, 220),
                                checked: checkbox instanceof HTMLInputElement
                                    ? checkbox.checked
                                    : checkbox.getAttribute('aria-checked') === 'true'
                            };
                        }
                    }
                }
                return null;
            };

            const allCandidates = Array.from(document.querySelectorAll(clickableSelector))
                .filter(isVisible)
                .map((element, index) => {
                    const displayText = getDisplayText(element);
                    const normalizedText = getNormalizedText(element);
                    const layerRoot = findLayerRoot(element);
                    const layerText = getDisplayText(layerRoot);
                    const layerChain = collectLayerChain(element);
                    const visibleActionsInLayer = layerRoot ? countVisibleActions(layerRoot) : [];
                    const exactConfirmationEvidence = getEvidenceCount(confirmationPatterns, displayText);
                    const tokenConfirmationEvidence = getEvidenceCount(confirmationTokenPatterns, displayText)
                        + getEvidenceCount(confirmationTokenPatterns, normalizedText);
                    const confirmationEvidence = exactConfirmationEvidence + tokenConfirmationEvidence;
                    const rootEvidence = getEvidenceCount(rootEvidencePatterns, layerText);
                    const fallbackEvidence = layerRoot
                        && rootEvidence > 0
                        && element.tagName.toLowerCase() === 'button'
                        && getEvidenceCount(negativeInteractivePatterns, displayText) === 0
                        ? 1
                        : 0;
                    const negativeEvidence = getEvidenceCount(negativePatterns, displayText);
                    const negativeInteractiveEvidence = getEvidenceCount(negativeInteractivePatterns, normalizedText);
                    const rect = element.getBoundingClientRect();
                    const marker = `${markerPrefix}-${index}`;
                    return {
                        element,
                        marker,
                        text: normalizedText,
                        displayText,
                        tagName: element.tagName.toLowerCase(),
                        role: element.getAttribute('role') || '',
                        href: element.getAttribute('href') || '',
                        confirmationEvidence,
                        rootEvidence,
                        fallbackEvidence,
                        negativeEvidence,
                        negativeInteractiveEvidence,
                        hasGateLayer: Boolean(layerRoot),
                        pointerReceivable: isPointerReceivable(element),
                        layerChain,
                        layerText: layerText.slice(0, 420),
                        visibleActionCountInLayer: visibleActionsInLayer.length,
                        area: rect.width * rect.height,
                        top: rect.top,
                        left: rect.left,
                    };
                });

            const candidates = allCandidates
                .filter((candidate) => candidate.hasGateLayer)
                .filter((candidate) => candidate.pointerReceivable)
                .filter((candidate) => candidate.confirmationEvidence > 0 || candidate.fallbackEvidence > 0)
                .filter((candidate) => candidate.negativeInteractiveEvidence === 0)
                .sort((left, right) => {
                    const comparisons = [
                        right.rootEvidence - left.rootEvidence,
                        right.confirmationEvidence - left.confirmationEvidence,
                        right.fallbackEvidence - left.fallbackEvidence,
                        left.negativeEvidence - right.negativeEvidence,
                        right.area - left.area,
                        right.top - left.top,
                        left.left - right.left,
                    ];
                    return comparisons.find((comparison) => comparison !== 0) || 0;
                });

            const summarize = (candidate) => ({
                text: candidate.text.slice(0, 300),
                displayText: candidate.displayText.slice(0, 260),
                tagName: candidate.tagName,
                role: candidate.role,
                href: candidate.href,
                confirmationEvidence: candidate.confirmationEvidence,
                rootEvidence: candidate.rootEvidence,
                fallbackEvidence: candidate.fallbackEvidence,
                negativeEvidence: candidate.negativeEvidence,
                negativeInteractiveEvidence: candidate.negativeInteractiveEvidence,
                hasGateLayer: candidate.hasGateLayer,
                pointerReceivable: candidate.pointerReceivable,
                layerChain: candidate.layerChain,
                layerText: candidate.layerText,
                visibleActionCountInLayer: candidate.visibleActionCountInLayer,
                area: candidate.area,
                top: candidate.top,
                left: candidate.left,
            });

            const visibleLayerDiagnostics = Array.from(document.querySelectorAll('body *'))
                .filter((element) => element instanceof HTMLElement)
                .filter(isVisible)
                .filter(isLayerElement)
                .map((element) => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return {
                        tagName: element.tagName.toLowerCase(),
                        role: element.getAttribute('role') || '',
                        id: element.id || '',
                        className: getClassText(element).slice(0, 300),
                        position: style.position,
                        zIndex: style.zIndex,
                        rect: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom },
                        text: getDisplayText(element).slice(0, 360),
                    };
                });

            if (candidates.length === 0) {
                return {
                    ok: false,
                    reason: 'No top-layer entry/update gate action was detected.',
                    candidates: allCandidates.map(summarize).slice(0, 32),
                    visibleLayerDiagnostics,
                };
            }

            const selectedCandidate = candidates[0];
            selectedCandidate.element.setAttribute('data-auto-wikigacha-entry', selectedCandidate.marker);
            const dontShowControl = findDontShowControl(selectedCandidate.element);
            return {
                ok: true,
                selector: `[data-auto-wikigacha-entry="${selectedCandidate.marker}"]`,
                dontShowControl,
                selected: summarize(selectedCandidate),
                candidates: candidates.map(summarize),
                visibleLayerDiagnostics,
            };
        }
        """
    )

def dismissEntryGates(page: Page, evidencePath: Path, rememberDismissal: bool) -> list[dict[str, Any]]:
    dismissedGates: list[dict[str, Any]] = []
    seenGateHashes: set[str] = set()

    while True:
        gateResolution = resolveEntryGateActionSelector(page)
        if not gateResolution.get("ok"):
            return dismissedGates

        gateHash = buildShortHash(json.dumps(gateResolution.get("selected", {}), ensure_ascii=False, sort_keys=True))
        if gateHash in seenGateHashes:
            gateResolution["repeatDetected"] = True
            dismissedGates.append(gateResolution)
            saveEvidence(page, evidencePath, "entry_gate_repeat_detected", gateResolution)
            return dismissedGates
        seenGateHashes.add(gateHash)

        saveEvidence(page, evidencePath, f"entry_gate_{len(dismissedGates) + 1:03d}_target", gateResolution)
        dontShowControl = gateResolution.get("dontShowControl")
        if rememberDismissal and dontShowControl and not dontShowControl.get("checked"):
            page.locator(dontShowControl["selector"]).click(timeout=0)
            waitForRenderCycle(page)
            refreshedGateResolution = resolveEntryGateActionSelector(page)
            if refreshedGateResolution.get("ok"):
                gateResolution = refreshedGateResolution
            else:
                return dismissedGates

        previousFingerprint = getPageFingerprint(page)
        page.locator(gateResolution["selector"]).click(timeout=0)
        renderObservation = waitForRenderCycle(page)
        currentFingerprint = getPageFingerprint(page)
        gateResolution["previousFingerprintHash"] = buildShortHash(previousFingerprint)
        gateResolution["currentFingerprintHash"] = buildShortHash(currentFingerprint)
        gateResolution["fingerprintChanged"] = previousFingerprint != currentFingerprint
        gateResolution["renderObservation"] = renderObservation
        dismissedGates.append(gateResolution)
        saveEvidence(page, evidencePath, f"entry_gate_{len(dismissedGates):03d}_dismissed", gateResolution)


def resolveDrawTargetSelector(page: Page) -> dict[str, Any]:
    return page.evaluate(
        r"""
        () => {
            const markerPrefix = `auto-wikigacha-target-${Date.now()}-${Math.random().toString(36).slice(2)}`;
            const candidateSelector = [
                'button',
                'a[href]',
                '[role="button"]',
                '[onclick]',
                '[tabindex]',
                '[id]',
                '[class]',
                'img[alt]'
            ].join(',');
            const positivePatterns = [
                /gacha/iu,
                /wiki\s*pack/iu,
                /pack/iu,
                /open/iu,
                /draw/iu,
                /pull/iu,
                /card/iu,
                /ガチャ/iu,
                /パック/iu,
                /開封/iu,
                /開く/iu,
                /引く/iu,
                /回す/iu,
                /抽/iu,
                /抽卡/iu,
                /召喚/iu,
                /卡包/iu,
                /開包/iu,
                /開啟/iu,
                /點擊開啟/iu
            ];
            const highConfidencePatterns = [
                /gacha-pack-container/iu,
                /wiki\s*pack/iu,
                /點擊開啟|点击开启/iu,
                /今日卡包/iu
            ];
            const negativePatterns = [
                /privacy|policy|terms|contact/iu,
                /wikipedia\.org/iu,
                /activity|campaign|event|details/iu,
                /活動詳情|活动详情|活動詳細|イベント詳細/iu,
                /server|sync|cloud|beta/iu,
                /伺服器同步|服务器同步|サーバー同期/iu,
                /language|語言|语言|言語/iu,
                /圖鑑|图鉴|図鑑/iu,
                /battle|對戰|对战|バトル/iu,
                /trophy|獎盃|奖杯/iu,
                /help|rule|說明|说明|遊戲說明|游戏说明/iu,
                /share|分享/iu,
                /ad|advertisement|廣告|广告/iu,
                /隱私|隐私|條款|条款|聯絡|联系|お問い合わせ/iu
            ];

            const getClassText = (element) => typeof element.className === 'string' ? element.className : '';
            const getNormalizedText = (element) => [
                element.innerText,
                element.textContent,
                element.getAttribute('aria-label'),
                element.getAttribute('title'),
                element.getAttribute('value'),
                element.id,
                getClassText(element),
                element.getAttribute('data-testid'),
                ...Array.from(element.querySelectorAll('img[alt]')).map((image) => image.getAttribute('alt'))
            ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();

            const isVisible = (element) => {
                if (!(element instanceof HTMLElement)) {
                    return false;
                }
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && rect.width > 0
                    && rect.height > 0
                    && !element.hasAttribute('disabled')
                    && element.getAttribute('aria-disabled') !== 'true';
            };

            const isPointerReceivable = (element) => {
                const rect = element.getBoundingClientRect();
                const centerX = rect.left + rect.width / 2;
                const centerY = rect.top + rect.height / 2;
                const hitElement = document.elementFromPoint(centerX, centerY);
                return Boolean(hitElement && (element === hitElement || element.contains(hitElement)));
            };

            const isInteractive = (element) => {
                const style = window.getComputedStyle(element);
                const tagName = element.tagName.toLowerCase();
                const classText = getClassText(element);
                return tagName === 'button'
                    || tagName === 'a'
                    || element.getAttribute('role') === 'button'
                    || element.hasAttribute('onclick')
                    || element.hasAttribute('tabindex')
                    || style.cursor === 'pointer'
                    || /(^|\s)cursor-pointer(\s|$)/u.test(classText);
            };

            const getEvidenceCount = (patterns, text) => patterns
                .map((pattern) => pattern.test(text))
                .filter(Boolean).length;

            const candidates = Array.from(document.querySelectorAll(candidateSelector))
                .filter(isVisible)
                .map((element, index) => {
                    const actionableElement = element.closest('button, a[href], [role="button"], [onclick], [tabindex], .cursor-pointer, [id*="gacha" i], [id*="pack" i]') || element;
                    const actionableText = getNormalizedText(actionableElement);
                    const elementText = getNormalizedText(element);
                    const combinedText = `${actionableText} ${elementText}`.replace(/\s+/g, ' ').trim();
                    const style = window.getComputedStyle(actionableElement);
                    const rect = actionableElement.getBoundingClientRect();
                    const positiveEvidence = getEvidenceCount(positivePatterns, combinedText);
                    const highConfidenceEvidence = getEvidenceCount(highConfidencePatterns, combinedText);
                    const negativeEvidence = getEvidenceCount(negativePatterns, combinedText);
                    const marker = `${markerPrefix}-${index}`;
                    return {
                        element: actionableElement,
                        marker,
                        text: combinedText,
                        tagName: actionableElement.tagName.toLowerCase(),
                        role: actionableElement.getAttribute('role') || '',
                        href: actionableElement.getAttribute('href') || '',
                        id: actionableElement.id || '',
                        positiveEvidence,
                        highConfidenceEvidence,
                        negativeEvidence,
                        pointerReceivable: isPointerReceivable(actionableElement),
                        interactive: isInteractive(actionableElement),
                        cursor: style.cursor,
                        area: rect.width * rect.height,
                        top: rect.top,
                        left: rect.left
                    };
                })
                .filter((candidate, index, allCandidates) => {
                    return index === allCandidates.findIndex((other) => other.element === candidate.element);
                })
                .filter((candidate) => candidate.pointerReceivable)
                .filter((candidate) => candidate.interactive)
                .filter((candidate) => candidate.positiveEvidence > 0 || candidate.highConfidenceEvidence > 0)
                .filter((candidate) => candidate.negativeEvidence === 0 || candidate.highConfidenceEvidence > 0)
                .sort((left, right) => {
                    const comparisons = [
                        right.highConfidenceEvidence - left.highConfidenceEvidence,
                        right.positiveEvidence - left.positiveEvidence,
                        left.negativeEvidence - right.negativeEvidence,
                        right.area - left.area,
                        left.top - right.top,
                        left.left - right.left
                    ];
                    return comparisons.find((comparison) => comparison !== 0) || 0;
                });

            const summarize = (candidate) => ({
                text: candidate.text.slice(0, 260),
                tagName: candidate.tagName,
                role: candidate.role,
                href: candidate.href,
                id: candidate.id,
                positiveEvidence: candidate.positiveEvidence,
                highConfidenceEvidence: candidate.highConfidenceEvidence,
                negativeEvidence: candidate.negativeEvidence,
                pointerReceivable: candidate.pointerReceivable,
                interactive: candidate.interactive,
                cursor: candidate.cursor,
                area: candidate.area,
                top: candidate.top,
                left: candidate.left
            });

            if (candidates.length === 0) {
                return {
                    ok: false,
                    reason: 'No visible, pointer-receivable draw target was found.',
                    candidates: [],
                    visibleTextSample: document.body ? document.body.innerText.slice(0, 1600) : '',
                };
            }

            const selectedCandidate = candidates[0];
            selectedCandidate.element.setAttribute('data-auto-wikigacha-target', selectedCandidate.marker);
            return {
                ok: true,
                selector: `[data-auto-wikigacha-target="${selectedCandidate.marker}"]`,
                selected: summarize(selectedCandidate),
                candidates: candidates.map(summarize)
            };
        }
        """
    )


def writeJson(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def collectNonSecretStorageSummary(page: Page) -> dict[str, Any]:
    return page.evaluate(
        r"""
        () => {
            const localStorageKeys = [];
            const localStorageValueLengths = {};
            for (let index = 0; index < localStorage.length; index += 1) {
                const key = localStorage.key(index);
                const value = localStorage.getItem(key) || '';
                localStorageKeys.push(key);
                localStorageValueLengths[key] = value.length;
            }
            return {
                href: location.href,
                title: document.title,
                localStorageKeys,
                localStorageValueLengths,
                userAgent: navigator.userAgent,
                language: navigator.language,
                languages: navigator.languages,
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


def recoverFromPossibleEntryGate(page: Page, evidencePath: Path, rememberDismissal: bool) -> list[dict[str, Any]]:
    gateResolution = resolveEntryGateActionSelector(page)
    saveEvidence(page, evidencePath, "draw_blocking_layer_diagnostics", gateResolution)
    if not gateResolution.get("ok"):
        return []
    return dismissEntryGates(page, evidencePath, rememberDismissal=rememberDismissal)


def runSetup(page: Page, targetUrl: str, evidencePath: Path, rememberDismissal: bool) -> None:
    page.goto(targetUrl, wait_until="domcontentloaded", timeout=0)
    waitForPageReady(page)
    dismissedGates = dismissEntryGates(page, evidencePath, rememberDismissal=rememberDismissal)
    print("[SETUP] 已嘗試自動處理入口／更新通知層。")
    print(f"[SETUP] Dismissed gate count: {len(dismissedGates)}")
    print("[SETUP] 請在開啟的瀏覽器中完成 Google 登入，並於站內手動啟用 server sync。")
    print("[SETUP] 完成後回到終端機按 Enter；這個持久化 profile 會在後續執行中沿用。")
    input()
    saveEvidence(page, evidencePath, "setup_complete", {"mode": "setup", "dismissedGates": dismissedGates})


def performDraws(page: Page, arguments: argparse.Namespace, evidencePath: Path) -> None:
    page.goto(arguments.url, wait_until="domcontentloaded", timeout=0)
    waitForPageReady(page)
    dismissedGates = dismissEntryGates(page, evidencePath, rememberDismissal=not arguments.keepEntryNotices)
    saveEvidence(
        page,
        evidencePath,
        "before_draw",
        {"drawCount": arguments.drawCount, "dismissedGates": dismissedGates},
    )

    for drawIndex in range(1, arguments.drawCount + 1):
        print(f"[INFO] Resolving draw target for draw {drawIndex}/{arguments.drawCount}")
        targetResolution = resolveDrawTargetSelector(page)
        if not targetResolution.get("ok"):
            recoveredGates = recoverFromPossibleEntryGate(
                page,
                evidencePath,
                rememberDismissal=not arguments.keepEntryNotices,
            )
            if recoveredGates:
                print(f"[INFO] Recovered from entry/update gate count: {len(recoveredGates)}")
                waitForRenderCycle(page)
                targetResolution = resolveDrawTargetSelector(page)

        if not targetResolution.get("ok"):
            saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_no_target", targetResolution)
            raise WikiGachaAutomationError(targetResolution.get("reason", "No draw target was found."))

        saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_target", targetResolution)
        if arguments.dryRun:
            print("[DRY-RUN] Target resolved; draw click skipped.")
            continue

        previousFingerprint = getPageFingerprint(page)
        page.locator(targetResolution["selector"]).click(timeout=0)
        renderObservation = waitForRenderCycle(page)
        currentFingerprint = getPageFingerprint(page)
        resultPayload = {
            "targetResolution": targetResolution,
            "previousFingerprintHash": buildShortHash(previousFingerprint),
            "currentFingerprintHash": buildShortHash(currentFingerprint),
            "fingerprintChanged": previousFingerprint != currentFingerprint,
            "renderObservation": renderObservation,
        }
        if previousFingerprint == currentFingerprint:
            saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_no_change_after_click", resultPayload)
            raise WikiGachaAutomationError(
                "Draw target was clicked, but the page fingerprint did not change after the render cycle. "
                "Run with --dryRun and inspect the target evidence JSON/screenshot."
            )
        saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_result", resultPayload)


def launchPersistentContext(playwright: Any, arguments: argparse.Namespace, profilePath: Path) -> BrowserContext:
    launchOptions: dict[str, Any] = {
        "user_data_dir": str(profilePath),
        "headless": arguments.headless,
        "locale": arguments.locale,
        "args": ["--start-maximized"],
        "no_viewport": True,
    }
    preferredChannel = arguments.browserChannel.strip() if arguments.browserChannel else ""
    if preferredChannel:
        try:
            context = playwright.chromium.launch_persistent_context(
                channel=preferredChannel,
                **launchOptions,
            )
            print(f"[INFO] Browser channel: {preferredChannel}")
            print(f"[INFO] Browser window visible: {not arguments.headless}")
            return context
        except PlaywrightError as error:
            print(
                f"[WARN] 無法使用 browser channel '{preferredChannel}'，改用 Playwright Chromium。原因：{error}",
                file=sys.stderr,
            )

    context = playwright.chromium.launch_persistent_context(**launchOptions)
    print("[INFO] Browser channel: chromium")
    print(f"[INFO] Browser window visible: {not arguments.headless}")
    return context


def main() -> int:
    arguments = parseArguments()
    ensureArgumentsAreValid(arguments)
    evidencePath = createEvidenceDirectory(arguments.evidenceDir)
    profilePath = Path(arguments.profileDir)
    profilePath.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = launchPersistentContext(playwright, arguments, profilePath)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            if arguments.setup:
                runSetup(page, arguments.url, evidencePath, rememberDismissal=not arguments.keepEntryNotices)
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
