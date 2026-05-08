from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    sync_playwright,
)

wikiGachaUrl = "https://wikigacha.com/?lang=ZH_HANT"
returnToPackPageButtonXPath = "/html/body/main/div/div/div[4]/div[1]/button"
remainingPackCountXPath = "/html/body/main/div/div/div[1]/div[1]/span"
insufficientPackHeadingXPath = "/html/body/main/div/div/div[1]/div/h2"
recoverPackButtonXPath = "/html/body/main/div/div/div[1]/div/button"
adRewardConfirmButtonXPath = "/html/body/main/div/div/div[2]/div/div/div[2]/button"
adOverlayCloseButtonXPath = "/html/body/div[1]/div[2]/div[4]/div[2]"


class WikiGachaAutomationError(RuntimeError):
    pass


class BrowserLifecycleRestartRequired(WikiGachaAutomationError):
    pass


saveRoutineEvidence = False
evidenceEventTrail: list[dict[str, Any]] = []


def setRoutineEvidenceEnabled(enabled: bool) -> None:
    global saveRoutineEvidence
    saveRoutineEvidence = enabled


def resetEvidenceEventTrail() -> None:
    evidenceEventTrail.clear()


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
        default=None,
        help=(
            "Optional maximum number of pack-opening lifecycles to complete. "
            "Omit this option to keep opening packs adaptively until the page-reported remaining pack "
            "count reaches zero; if that count is unavailable, the script falls back to pack-target presence."
        ),
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
        "--saveRoutineEvidence",
        action="store_true",
        help=(
            "Persist routine screenshots and non-secret reports during normal operation. "
            "By default, evidence files are written only when an error occurs."
        ),
    )
    parser.add_argument(
        "--executionMode",
        choices=("bot", "manual"),
        default=None,
        help=(
            "Execution mode override. Omit this option to choose interactively at startup: "
            "press Enter or type 1 for Bot mode, type 2 for Manual mode. "
            "Manual mode launches an ordinary Chrome process instead of a Playwright-controlled browser."
        ),
    )
    parser.add_argument(
        "--externalChromePath",
        default=None,
        help=(
            "Optional path to an installed Google Chrome executable used by Manual/setup mode. "
            "When omitted, the script searches common Chrome executable locations and PATH."
        ),
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
        help=(
            "Open the site in an ordinary, non-Playwright-controlled Chrome process using the same persistent "
            "profile directory, then pause for manual Google sign-in / server-sync setup."
        ),
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
    parser.add_argument(
        "--returnToPackPageXPath",
        default=returnToPackPageButtonXPath,
        help=(
            "XPath for the button that returns from an opened pack/result view back to the pack page. "
            "The default is the current Traditional Chinese layout path supplied for Wikipedia Gacha."
        ),
    )
    parser.add_argument(
        "--remainingPackCountXPath",
        default=remainingPackCountXPath,
        help=(
            "XPath for the pack-page element that reports how many packs remain. "
            "The default targets the current Traditional Chinese layout's 今日卡包 counter."
        ),
    )
    parser.add_argument(
        "--insufficientPackHeadingXPath",
        default=insufficientPackHeadingXPath,
        help=(
            "XPath for the heading that indicates the card-pack shortage state. "
            "The resolver also uses semantic fallback detection when this XPath is unavailable."
        ),
    )
    parser.add_argument(
        "--recoverPackButtonXPath",
        default=recoverPackButtonXPath,
        help=(
            "XPath for the shortage-state button that starts ad-based pack recovery. "
            "The resolver also uses semantic fallback detection when this XPath is unavailable."
        ),
    )
    parser.add_argument(
        "--adRewardConfirmButtonXPath",
        default=adRewardConfirmButtonXPath,
        help=(
            "XPath for the post-ad reward confirmation button. "
            "The resolver waits adaptively for this control and falls back to semantic dialog buttons."
        ),
    )
    parser.add_argument(
        "--adOverlayCloseButtonXPath",
        default=adOverlayCloseButtonXPath,
        help=(
            "XPath for the rewarded-ad overlay close button. "
            "The default targets the current Google rewarded overlay's round close button; "
            "semantic close-ad detection remains available when this XPath changes."
        ),
    )
    parser.add_argument(
        "--adInterruptionRecoveryRestartSeconds",
        type=float,
        default=40.0,
        help=(
            "Observed-time policy for restarting the current browser lifecycle when ad-interruption close recovery "
            "keeps cycling without reaching a pack-ready or reward-confirmation state. Keep the default to match "
            "the requested forty-second operational boundary; tune this value instead of editing control-flow code."
        ),
    )
    parser.add_argument(
        "--googleRewardedAdCloseSettlingSeconds",
        type=float,
        default=10.0,
        help=(
            "Observed post-click settling window for Google rewarded-ad close controls such as "
            "reward_close_button_widget / close_button / close_button_icon. The default follows the requested "
            "ten-second operational pause, while keeping the value configurable instead of hard-coding it in "
            "the recovery control flow."
        ),
    )
    parser.add_argument(
        "--keepBrowserOpenAfterAdaptiveCompletion",
        action="store_true",
        help=(
            "Compatibility switch. By default, an unbounded adaptive run that reaches the completion state closes "
            "the current automated Chrome page/context and starts a fresh bot lifecycle. Enable this flag only when "
            "you explicitly want the old passive behavior of keeping the completed browser open."
        ),
    )
    return parser.parse_args()


def ensureArgumentsAreValid(arguments: argparse.Namespace) -> None:
    if arguments.drawCount is not None and arguments.drawCount < 1:
        raise ValueError("--drawCount 必須大於 0。")
    if arguments.adInterruptionRecoveryRestartSeconds <= 0:
        raise ValueError("--adInterruptionRecoveryRestartSeconds 必須大於 0。")
    if arguments.googleRewardedAdCloseSettlingSeconds < 0:
        raise ValueError("--googleRewardedAdCloseSettlingSeconds 不可小於 0。")


def formatDrawProgress(drawIndex: int, drawCount: int | None) -> str:
    if drawCount is None:
        return f"{drawIndex}/auto"
    return f"{drawIndex}/{drawCount}"


def getDrawRunMode(arguments: argparse.Namespace) -> str:
    if arguments.drawCount is None:
        return "untilRemainingPackCountIsZero"
    return "boundedDrawCountWithRemainingPackGuard"


def normalizeExecutionModeSelection(selectionText: str) -> str | None:
    normalizedSelection = selectionText.strip().lower()
    if normalizedSelection == "":
        return "bot"
    botSelections = {"1", "bot", "b", "auto", "automatic", "自動", "自動模式"}
    manualSelections = {"2", "manual", "m", "手動", "手動模式"}
    if normalizedSelection in botSelections:
        return "bot"
    if normalizedSelection in manualSelections:
        return "manual"
    return None


def promptForExecutionMode() -> str:
    if not sys.stdin.isatty():
        print("[INFO] No interactive stdin was detected; defaulting to Bot mode.")
        return "bot"

    while True:
        print("\n請選擇執行模式：")
        print("  [1] Bot 模式：自動抽卡與自動處理廣告恢復流程")
        print("  [2] Manual 模式：用一般 Chrome 開啟網站，給你手動登入 Google／伺服器同步")
        selectionText = input("請輸入模式數字後按 Enter；直接按 Enter 預設 Bot 模式：")
        executionMode = normalizeExecutionModeSelection(selectionText)
        if executionMode:
            return executionMode
        print("[WARN] 無法辨識模式選擇。請輸入 1、2，或直接按 Enter 使用 Bot 模式。", file=sys.stderr)


def resolveExecutionMode(arguments: argparse.Namespace) -> str:
    if arguments.executionMode:
        return arguments.executionMode
    return promptForExecutionMode()


def createEvidenceDirectory(evidenceDir: str, shouldCreateImmediately: bool = False) -> Path:
    evidencePath = Path(evidenceDir)
    if shouldCreateImmediately:
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
                bodyMarkup: document.body ? document.body.innerHTML : "",
                storageEntries,
            });
        }
        """
    )


def getRenderedStateFingerprint(page: Page) -> str:
    return page.evaluate(
        r"""
        () => {
            const normalizeWhitespace = (value) => String(value || '').replace(/\s+/g, ' ').trim();
            const isRenderedElement = (element) => {
                if (!(element instanceof HTMLElement)) {
                    return false;
                }
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && rect.width > 0
                    && rect.height > 0;
            };
            const summarizeElement = (element) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return {
                    tagName: element.tagName.toLowerCase(),
                    id: element.id || '',
                    className: typeof element.className === 'string' ? element.className : '',
                    role: element.getAttribute('role') || '',
                    ariaHidden: element.getAttribute('aria-hidden') || '',
                    text: normalizeWhitespace(element.innerText || element.textContent || ''),
                    rect: {
                        left: rect.left,
                        top: rect.top,
                        right: rect.right,
                        bottom: rect.bottom,
                        width: rect.width,
                        height: rect.height,
                    },
                    display: style.display,
                    visibility: style.visibility,
                    opacity: style.opacity,
                    transform: style.transform,
                    pointerEvents: style.pointerEvents,
                    zIndex: style.zIndex,
                };
            };

            return JSON.stringify({
                href: location.href,
                title: document.title,
                scrollX: window.scrollX,
                scrollY: window.scrollY,
                viewportWidth: window.innerWidth,
                viewportHeight: window.innerHeight,
                renderedElements: Array.from(document.querySelectorAll('body *'))
                    .filter(isRenderedElement)
                    .map(summarizeElement),
            });
        }
        """
    )


def getPageStateFingerprint(page: Page) -> str:
    return json.dumps(
        {
            "pageFingerprint": getPageFingerprint(page),
            "renderedStateFingerprint": getRenderedStateFingerprint(page),
        },
        ensure_ascii=False,
        sort_keys=True,
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
            let observedFiniteAnimationCount = 0;
            let completedFiniteAnimationCount = 0;
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

            const requestPostPaintCallback = (callback) => {
                window.requestAnimationFrame(() => {
                    window.requestAnimationFrame(callback);
                });
            };

            const getFiniteRunningAnimations = () => document
                .getAnimations({ subtree: true })
                .filter((animation) => {
                    const timing = animation.effect && typeof animation.effect.getComputedTiming === 'function'
                        ? animation.effect.getComputedTiming()
                        : null;
                    const hasFiniteTimeline = !timing || Number.isFinite(timing.endTime);
                    return hasFiniteTimeline
                        && (animation.playState === 'pending' || animation.playState === 'running');
                });

            const finish = () => {
                requestPostPaintCallback(() => {
                    observer.disconnect();
                    resolve({
                        mutationCount,
                        observedFiniteAnimationCount,
                        completedFiniteAnimationCount,
                        settledBy: 'finiteAnimations+postPaint',
                    });
                });
            };

            const activeAnimations = getFiniteRunningAnimations();
            if (activeAnimations.length === 0) {
                finish();
                return;
            }

            observedFiniteAnimationCount += activeAnimations.length;
            Promise.allSettled(activeAnimations.map((animation) => animation.finished))
                .then((results) => {
                    completedFiniteAnimationCount += results.length;
                    finish();
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
            const primaryDismissPatterns = [
                /^(知道了|我知道了|確定|確認|關閉|OK)$/iu,
                /^(知道了|我知道了|确定|确认|关闭|OK)$/iu,
                /^(got it|ok|okay|close|dismiss|continue)$/iu,
                /^(了解|閉じる|確認|OK)$/iu,
            ];
            const rootEvidencePatterns = [
                /更新通知|公告|通知|入口|歡迎|欢迎|說明|说明/iu,
                /紀念活動|纪念活动|活動進行中|活动进行中|活動.*進行|活动.*进行/iu,
                /慶祝|庆祝|累計開啟|累计开启|特別卡包|特别卡包|登入章|登錄章|登录章/iu,
                /notice|announcement|update|welcome|entry|modal|dialog/iu,
                /commemorative|campaign|event\s*(?:ongoing|notice|reward)|special\s*pack/iu,
                /お知らせ|更新|通知|案内|記念|イベント/iu,
            ];
            const negativePatterns = [
                /activity\s*details|event\s*details|campaign\s*details/iu,
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
                /share|sharing|tweet|post/iu,
                /分享|轉發|转发|シェア/iu,
                /activity\s*details|event\s*details|campaign\s*details/iu,
                /活動詳情|活动详情|活動詳細|イベント詳細/iu,
                /server|sync|cloud|beta/iu,
                /伺服器同步|服务器同步|サーバー同期/iu,
                /language|語言|语言|言語/iu,
                /privacy|policy|terms|contact/iu,
                /隱私|隐私|條款|条款|聯絡|联系/iu,
                /圖鑑|图鉴|對戰|对战|獎盃|奖杯/iu,
            ];
            const positiveDontShowPatterns = [
                /到明天前不再顯示|到明天前不再显示/iu,
                /明天.*不再顯示|明天.*不再显示/iu,
                /下次不再顯示|下次不再显示|不再顯示|不再显示/iu,
                /don't show again|do not show again|hide until tomorrow/iu,
                /次回から表示しない|明日まで表示しない/iu
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

            const getEvidenceCount = (patterns, text) => patterns
                .map((pattern) => pattern.test(text))
                .filter(Boolean).length;

            const countVisibleActions = (root) => Array.from(root.querySelectorAll(clickableSelector))
                .filter(isVisible)
                .filter((element) => !element.matches('input[type="checkbox"], [role="checkbox"]'));

            const isLayerElement = (element) => {
                if (!(element instanceof HTMLElement)) {
                    return false;
                }
                const style = window.getComputedStyle(element);
                const role = element.getAttribute('role') || '';
                const classText = getClassText(element);
                const rect = element.getBoundingClientRect();
                const classIndicatesGateLayer = /(^|\s)(fixed|modal|dialog|overlay|popover|inset-0|z-\d+)(\s|$)/iu.test(classText);
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

            const findSemanticGateRoot = (element) => {
                let current = element;
                let semanticRoot = null;
                while (current && current instanceof HTMLElement && current !== document.body) {
                    const text = getDisplayText(current);
                    const visibleActionCount = countVisibleActions(current).length;
                    const rootEvidence = getEvidenceCount(rootEvidencePatterns, text);
                    const confirmationActionExists = countVisibleActions(current).some((action) => {
                        const actionText = getDisplayText(action);
                        return getEvidenceCount(confirmationPatterns, actionText) > 0
                            || getEvidenceCount(confirmationTokenPatterns, actionText) > 0;
                    });
                    if (rootEvidence > 0 && visibleActionCount > 0 && confirmationActionExists) {
                        semanticRoot = current;
                    }
                    current = current.parentElement;
                }
                return semanticRoot;
            };

            const findGateRoot = (element) => findSemanticGateRoot(element) || findLayerRoot(element);

            const findDontShowControl = (element) => {
                const searchRoot = findGateRoot(element) || document.body;
                const controls = Array.from(searchRoot.querySelectorAll('label, input[type="checkbox"], [role="checkbox"]'));
                for (const control of controls) {
                    const text = getDisplayText(control.parentElement || control);
                    if (positiveDontShowPatterns.some((pattern) => pattern.test(text))) {
                        const checkbox = control.matches('input[type="checkbox"], [role="checkbox"]')
                            ? control
                            : control.querySelector('input[type="checkbox"], [role="checkbox"]');
                        const clickTarget = checkbox instanceof HTMLElement && isVisible(checkbox)
                            ? checkbox
                            : control instanceof HTMLElement && isVisible(control)
                                ? control
                                : null;
                        if (clickTarget) {
                            const marker = `${markerPrefix}-dont-show`;
                            clickTarget.setAttribute('data-auto-wikigacha-entry', marker);
                            return {
                                selector: `[data-auto-wikigacha-entry="${marker}"]`,
                                text: text.slice(0, 220),
                                checked: checkbox instanceof HTMLInputElement
                                    ? checkbox.checked
                                    : checkbox instanceof HTMLElement
                                        ? checkbox.getAttribute('aria-checked') === 'true'
                                        : false
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
                    const gateRoot = findGateRoot(element);
                    const layerChain = collectLayerChain(element);
                    const gateText = getDisplayText(gateRoot);
                    const visibleActionsInGate = gateRoot ? countVisibleActions(gateRoot) : [];
                    const exactConfirmationEvidence = getEvidenceCount(confirmationPatterns, displayText);
                    const tokenConfirmationEvidence = getEvidenceCount(confirmationTokenPatterns, displayText)
                        + getEvidenceCount(confirmationTokenPatterns, normalizedText);
                    const primaryDismissEvidence = getEvidenceCount(primaryDismissPatterns, displayText);
                    const confirmationEvidence = exactConfirmationEvidence + tokenConfirmationEvidence + primaryDismissEvidence;
                    const rootEvidence = getEvidenceCount(rootEvidencePatterns, gateText);
                    const fallbackEvidence = gateRoot
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
                        exactConfirmationEvidence,
                        tokenConfirmationEvidence,
                        primaryDismissEvidence,
                        confirmationEvidence,
                        rootEvidence,
                        fallbackEvidence,
                        negativeEvidence,
                        negativeInteractiveEvidence,
                        hasGateLayer: Boolean(gateRoot),
                        pointerReceivable: isPointerReceivable(element),
                        layerChain,
                        layerText: gateText.slice(0, 420),
                        visibleActionCountInLayer: visibleActionsInGate.length,
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
                        right.primaryDismissEvidence - left.primaryDismissEvidence,
                        right.rootEvidence - left.rootEvidence,
                        right.exactConfirmationEvidence - left.exactConfirmationEvidence,
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
                exactConfirmationEvidence: candidate.exactConfirmationEvidence,
                tokenConfirmationEvidence: candidate.tokenConfirmationEvidence,
                primaryDismissEvidence: candidate.primaryDismissEvidence,
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
                .filter((element) => isLayerElement(element) || getEvidenceCount(rootEvidencePatterns, getDisplayText(element)) > 0)
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
                    reason: 'No top-layer entry/update/event gate action was detected.',
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


def resolveDrawTargetSelector(page: Page, returnButtonXPath: str) -> dict[str, Any]:
    return page.evaluate(
        r"""
        (returnButtonXPath) => {
            const markerPrefix = `auto-wikigacha-target-${Date.now()}-${Math.random().toString(36).slice(2)}`;
            const candidateSelector = [
                'button',
                'a[href]',
                '[role="button"]',
                '[onclick]',
                '[tabindex]',
                '.cursor-pointer',
                '[id*="gacha" i]',
                '[id*="pack" i]',
                '[class*="gacha" i]',
                '[class*="pack" i]',
                '[class*="card-back" i]',
                '[class*="flip-card" i]',
                '[class*="reveal" i]'
            ].join(',');
            const actionableClosestSelector = [
                'button',
                'a[href]',
                '[role="button"]',
                '[onclick]',
                '[tabindex]',
                '.cursor-pointer',
                '[id*="gacha" i]',
                '[id*="pack" i]',
                '[class*="gacha" i]',
                '[class*="pack" i]',
                '[class*="card-back" i]',
                '[class*="flip-card" i]',
                '[class*="reveal" i]'
            ].join(',');
            const positivePatterns = [
                /gacha/iu,
                /wiki\s*pack/iu,
                /pack/iu,
                /open\s*(?:pack|card)/iu,
                /draw|pull/iu,
                /reveal|flip/iu,
                /ガチャ|パック|開封|開く|引く|回す/iu,
                /抽卡|抽|召喚|卡包|開包|開啟|點擊開啟|點擊.*(?:開|翻|揭)/iu,
                /点击开启|点击.*(?:开|翻|揭)/iu
            ];
            const highConfidencePatterns = [
                /gacha-pack-container/iu,
                /pack[-_\s]*(?:opening|container|target|button)/iu,
                /card[-_\s]*(?:back|container|unrevealed|hidden|face[-_\s]*down)/iu,
                /flip[-_\s]*card|reveal[-_\s]*(?:card|target)/iu,
                /unrevealed|face[-_\s]*down|card\s*back/iu,
                /點擊開啟|点击开启|未翻|未開|未开|背面|卡背/iu,
                /今日卡包/iu
            ];
            const returnPatterns = [
                /返回卡包頁面|返回卡包页面|回到卡包|回卡包/iu,
                /返回.*卡包|卡包.*返回/iu,
                /return\s+to\s+pack|back\s+to\s+pack|pack\s+page/iu,
                /パック.*戻|戻.*パック/iu
            ];
            const resultUtilityPatterns = [
                /複製結果|复制结果|分享結果|分享结果/iu,
                /copy\s+result|share\s+result/iu,
                /左右滑動|左右滑动|使用\s*<>\s*翻頁|使用\s*<>\s*翻页/iu,
                /內容遵循|内容遵循|Wikipedia 作者所有|ATK|DEF/iu,
                /分享|share|シェア|複製|复制|copy/iu,
                /結果|结果|result/iu
            ];
            const carouselControlPatterns = [
                /^\s*[<>‹›«»]\s*$/u,
                /上一張|下一張|上一张|下一张|previous|next/iu,
                /carousel|slider|swiper|slide/iu,
                /翻頁|翻页|ページ/iu
            ];
            const negativePatterns = [
                ...returnPatterns,
                ...resultUtilityPatterns,
                ...carouselControlPatterns,
                /privacy|policy|terms|contact/iu,
                /wikipedia\.org/iu,
                /activity\s*details|campaign\s*details|event\s*details/iu,
                /活動詳情|活动详情|活動詳細|イベント詳細/iu,
                /server|sync|cloud|beta/iu,
                /伺服器同步|服务器同步|サーバー同期/iu,
                /language|語言|语言|言語/iu,
                /圖鑑|图鉴|図鑑/iu,
                /battle|對戰|对战|バトル/iu,
                /trophy|獎盃|奖杯/iu,
                /help|rule|說明|说明|遊戲說明|游戏说明/iu,
                /ad|advertisement|廣告|广告/iu,
                /隱私|隐私|條款|条款|聯絡|联系|お問い合わせ/iu
            ];

            const getClassText = (element) => typeof element.className === 'string' ? element.className : '';
            const normalizeWhitespace = (value) => String(value || '').replace(/\s+/g, ' ').trim();
            const getNormalizedText = (element) => {
                if (!element) {
                    return '';
                }
                return normalizeWhitespace([
                    element.innerText,
                    element.textContent,
                    element.getAttribute ? element.getAttribute('aria-label') : '',
                    element.getAttribute ? element.getAttribute('title') : '',
                    element.getAttribute ? element.getAttribute('value') : '',
                    element.id,
                    getClassText(element),
                    element.getAttribute ? element.getAttribute('data-testid') : '',
                    ...Array.from(element.querySelectorAll ? element.querySelectorAll('img[alt]') : [])
                        .map((image) => image.getAttribute('alt'))
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
                const rectangles = Array.from(element.getClientRects()).filter((rect) => rect.width > 0 && rect.height > 0);
                const targetRectangles = rectangles.length > 0 ? rectangles : [element.getBoundingClientRect()];
                return targetRectangles.some((rect) => {
                    const centerX = rect.left + rect.width / 2;
                    const centerY = rect.top + rect.height / 2;
                    const hitElement = document.elementFromPoint(centerX, centerY);
                    return Boolean(hitElement && (element === hitElement || element.contains(hitElement) || hitElement.contains(element)));
                });
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
            const resolveXPathElement = (xpath) => {
                if (!xpath) {
                    return null;
                }
                try {
                    return document.evaluate(
                        xpath,
                        document,
                        null,
                        XPathResult.FIRST_ORDERED_NODE_TYPE,
                        null,
                    ).singleNodeValue;
                } catch (error) {
                    return null;
                }
            };

            const configuredReturnButton = resolveXPathElement(returnButtonXPath);
            const isReturnButtonOrInside = (element) => configuredReturnButton
                && (element === configuredReturnButton || configuredReturnButton.contains(element));
            const visibleBodyText = document.body ? getNormalizedText(document.body) : '';
            const pageResultEvidence = getEvidenceCount(returnPatterns, visibleBodyText)
                + getEvidenceCount(resultUtilityPatterns, visibleBodyText);

            const allCandidates = Array.from(document.querySelectorAll(candidateSelector))
                .filter((element) => element instanceof HTMLElement)
                .filter(isVisible)
                .map((element, index) => {
                    const actionableElement = element.closest(actionableClosestSelector) || element;
                    const actionableText = getNormalizedText(actionableElement);
                    const elementText = getNormalizedText(element);
                    const combinedText = normalizeWhitespace(`${actionableText} ${elementText}`);
                    const style = window.getComputedStyle(actionableElement);
                    const rect = actionableElement.getBoundingClientRect();
                    const positiveEvidence = getEvidenceCount(positivePatterns, combinedText);
                    const highConfidenceEvidence = getEvidenceCount(highConfidencePatterns, combinedText);
                    const negativeEvidence = getEvidenceCount(negativePatterns, combinedText);
                    const resultUtilityEvidence = getEvidenceCount(resultUtilityPatterns, combinedText);
                    const carouselControlEvidence = getEvidenceCount(carouselControlPatterns, combinedText);
                    const returnEvidence = getEvidenceCount(returnPatterns, combinedText);
                    const marker = `${markerPrefix}-${index}`;
                    return {
                        element: actionableElement,
                        marker,
                        text: combinedText,
                        tagName: actionableElement.tagName.toLowerCase(),
                        role: actionableElement.getAttribute('role') || '',
                        href: actionableElement.getAttribute('href') || '',
                        id: actionableElement.id || '',
                        className: getClassText(actionableElement).slice(0, 260),
                        positiveEvidence,
                        highConfidenceEvidence,
                        negativeEvidence,
                        resultUtilityEvidence,
                        carouselControlEvidence,
                        returnEvidence,
                        pageResultEvidence,
                        pointerReceivable: isPointerReceivable(actionableElement),
                        interactive: isInteractive(actionableElement),
                        cursor: style.cursor,
                        area: rect.width * rect.height,
                        top: rect.top,
                        left: rect.left,
                        isConfiguredReturnButton: isReturnButtonOrInside(actionableElement),
                    };
                })
                .filter((candidate, index, candidates) => {
                    return index === candidates.findIndex((other) => other.element === candidate.element);
                });

            const candidates = allCandidates
                .filter((candidate) => !candidate.isConfiguredReturnButton)
                .filter((candidate) => candidate.returnEvidence === 0)
                .filter((candidate) => candidate.resultUtilityEvidence === 0)
                .filter((candidate) => candidate.carouselControlEvidence === 0)
                .filter((candidate) => candidate.pointerReceivable)
                .filter((candidate) => candidate.interactive)
                .filter((candidate) => candidate.positiveEvidence > 0 || candidate.highConfidenceEvidence > 0)
                .filter((candidate) => candidate.negativeEvidence === 0 || candidate.highConfidenceEvidence > 0)
                .filter((candidate) => pageResultEvidence === 0 || candidate.highConfidenceEvidence > 0)
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
                className: candidate.className,
                positiveEvidence: candidate.positiveEvidence,
                highConfidenceEvidence: candidate.highConfidenceEvidence,
                negativeEvidence: candidate.negativeEvidence,
                resultUtilityEvidence: candidate.resultUtilityEvidence,
                carouselControlEvidence: candidate.carouselControlEvidence,
                returnEvidence: candidate.returnEvidence,
                pageResultEvidence: candidate.pageResultEvidence,
                pointerReceivable: candidate.pointerReceivable,
                interactive: candidate.interactive,
                cursor: candidate.cursor,
                area: candidate.area,
                top: candidate.top,
                left: candidate.left,
                isConfiguredReturnButton: candidate.isConfiguredReturnButton,
            });

            if (candidates.length === 0) {
                return {
                    ok: false,
                    reason: pageResultEvidence > 0
                        ? 'Result-page controls were detected, so pack/card continuation targets were suppressed to avoid clicking revealed cards or carousel/result utility controls.'
                        : 'No visible, pointer-receivable pack/card continuation target was found.',
                    candidates: allCandidates.map(summarize).slice(0, 48),
                    pageResultEvidence,
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
        """,
        returnButtonXPath,
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


def buildEvidencePayload(page: Page, label: str, extraPayload: dict[str, Any]) -> dict[str, Any]:
    timestampText = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return {
        "createdAtUtc": timestampText,
        "label": label,
        "url": page.url,
        "storageSummary": collectNonSecretStorageSummary(page),
        "extra": extraPayload,
    }


def persistEvidencePayload(
    page: Page,
    evidencePath: Path,
    payload: dict[str, Any],
    screenshotLabel: str | None = None,
) -> tuple[Path, Path]:
    evidencePath.mkdir(parents=True, exist_ok=True)
    timestampText = payload.get("createdAtUtc") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = screenshotLabel or str(payload.get("label") or "evidence")
    screenshotPath = evidencePath / f"{timestampText}_{label}.png"
    reportPath = evidencePath / f"{timestampText}_{label}.json"
    page.screenshot(path=str(screenshotPath), full_page=True)
    writeJson(reportPath, payload)
    print(f"[INFO] Saved screenshot: {screenshotPath}")
    print(f"[INFO] Saved non-secret report: {reportPath}")
    return screenshotPath, reportPath


def summarizeEvidenceEvent(payload: dict[str, Any]) -> dict[str, Any]:
    extraPayload = payload.get("extra", {})
    serializedExtraPayload = json.dumps(extraPayload, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "createdAtUtc": payload.get("createdAtUtc"),
        "label": payload.get("label"),
        "url": payload.get("url"),
        "extraPayloadHash": buildShortHash(serializedExtraPayload),
        "extraPayloadKeys": sorted(extraPayload.keys()) if isinstance(extraPayload, dict) else [],
    }


def saveEvidence(page: Page, evidencePath: Path, label: str, extraPayload: dict[str, Any]) -> None:
    if not saveRoutineEvidence:
        timestampText = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        serializedExtraPayload = json.dumps(extraPayload, ensure_ascii=False, sort_keys=True, default=str)
        evidenceEventTrail.append(
            {
                "createdAtUtc": timestampText,
                "label": label,
                "url": page.url,
                "extraPayloadHash": buildShortHash(serializedExtraPayload),
                "extraPayloadKeys": sorted(extraPayload.keys()) if isinstance(extraPayload, dict) else [],
            }
        )
        return

    payload = buildEvidencePayload(page, label, extraPayload)
    evidenceEventTrail.append(summarizeEvidenceEvent(payload))
    persistEvidencePayload(page, evidencePath, payload)


def saveErrorEvidence(
    page: Page,
    evidencePath: Path,
    error: BaseException,
    arguments: argparse.Namespace,
) -> None:
    errorPayload = buildEvidencePayload(
        page,
        "error",
        {
            "errorType": type(error).__name__,
            "errorMessage": str(error),
            "drawRunMode": getDrawRunMode(arguments),
            "url": arguments.url,
            "profileDir": arguments.profileDir,
            "routineEvidencePersisted": saveRoutineEvidence,
            "evidenceEventTrail": evidenceEventTrail,
        },
    )
    persistEvidencePayload(page, evidencePath, errorPayload)


adaptiveAdInterruptionRecoveryState: dict[str, Any] = {}


def resetAdaptiveAdInterruptionRecoveryState(reason: str | None = None) -> None:
    adaptiveAdInterruptionRecoveryState.clear()
    if reason:
        adaptiveAdInterruptionRecoveryState["lastResetReason"] = reason


def summarizeAdInterruptionResolutionForRestart(resolution: dict[str, Any]) -> dict[str, Any]:
    return {
        "targetScope": resolution.get("targetScope", "page"),
        "selector": resolution.get("selector", ""),
        "frameIndex": resolution.get("frameIndex"),
        "frameName": resolution.get("frameName"),
        "frameUrlHash": buildShortHash(str(resolution.get("frameUrl", ""))),
        "selected": resolution.get("selected"),
        "reason": resolution.get("reason"),
    }


def buildAdInterruptionRestartEvidence(
    page: Page,
    drawIndex: int,
    adInterruptionResolution: dict[str, Any],
    adInterruptionClickPayload: dict[str, Any],
) -> dict[str, Any]:
    renderedStateFingerprint = getRenderedStateFingerprint(page)
    return {
        "drawIndex": drawIndex,
        "url": page.url,
        "adInterruptionResolutionFingerprint": buildResolutionFingerprint(adInterruptionResolution),
        "adInterruptionResolutionSummary": summarizeAdInterruptionResolutionForRestart(adInterruptionResolution),
        "clickStateChanged": adInterruptionClickPayload.get("stateChanged"),
        "pageFingerprintChanged": adInterruptionClickPayload.get("pageFingerprintChanged"),
        "renderedStateChanged": adInterruptionClickPayload.get("renderedStateChanged"),
        "currentFingerprintHash": adInterruptionClickPayload.get("currentFingerprintHash"),
        "currentRenderedStateFingerprintHash": buildShortHash(renderedStateFingerprint),
        "renderObservation": adInterruptionClickPayload.get("renderObservation"),
    }


def recordAdInterruptionRecoveryAndRestartIfStalled(
    page: Page,
    evidencePath: Path,
    arguments: argparse.Namespace,
    drawIndex: int,
    adInterruptionResolution: dict[str, Any],
    adInterruptionClickPayload: dict[str, Any],
) -> None:
    nowMonotonicSeconds = time.monotonic()
    eventSummary = buildAdInterruptionRestartEvidence(
        page,
        drawIndex,
        adInterruptionResolution,
        adInterruptionClickPayload,
    )

    currentDrawIndex = adaptiveAdInterruptionRecoveryState.get("drawIndex")
    if currentDrawIndex != drawIndex or "startedAtMonotonicSeconds" not in adaptiveAdInterruptionRecoveryState:
        adaptiveAdInterruptionRecoveryState.clear()
        adaptiveAdInterruptionRecoveryState.update(
            {
                "drawIndex": drawIndex,
                "startedAtMonotonicSeconds": nowMonotonicSeconds,
                "startedAtUtc": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                "events": [],
            }
        )

    recoveryEvents = adaptiveAdInterruptionRecoveryState.setdefault("events", [])
    eventSummary["eventIndex"] = len(recoveryEvents) + 1
    eventSummary["elapsedRecoverySeconds"] = nowMonotonicSeconds - adaptiveAdInterruptionRecoveryState["startedAtMonotonicSeconds"]
    recoveryEvents.append(eventSummary)

    elapsedRecoverySeconds = eventSummary["elapsedRecoverySeconds"]
    hasRepeatedAdInterruptionClose = len(recoveryEvents) > 1
    shouldRestartLifecycle = (
        hasRepeatedAdInterruptionClose
        and elapsedRecoverySeconds >= arguments.adInterruptionRecoveryRestartSeconds
    )
    if not shouldRestartLifecycle:
        return

    restartPayload = {
        "reason": (
            "Ad-interruption close recovery kept cycling without reaching a pack-ready or reward-confirmation state; "
            "the current page will be closed and a fresh browser lifecycle will be started."
        ),
        "drawIndex": drawIndex,
        "elapsedRecoverySeconds": elapsedRecoverySeconds,
        "adInterruptionRecoveryRestartSeconds": arguments.adInterruptionRecoveryRestartSeconds,
        "recoveryEventCount": len(recoveryEvents),
        "recoveryEvents": recoveryEvents,
        "latestEvent": eventSummary,
        "drawRunMode": getDrawRunMode(arguments),
        "url": arguments.url,
        "profileDir": arguments.profileDir,
    }
    payload = buildEvidencePayload(
        page,
        f"draw_{drawIndex:03d}_ad_interruption_recovery_restart_requested",
        restartPayload,
    )
    evidenceEventTrail.append(summarizeEvidenceEvent(payload))
    persistEvidencePayload(page, evidencePath, payload)
    raise BrowserLifecycleRestartRequired(restartPayload["reason"])

def resolveRemainingPackCount(
    page: Page,
    remainingCountXPath: str,
    insufficientPackHeadingXPathValue: str = insufficientPackHeadingXPath,
) -> dict[str, Any]:
    return page.evaluate(
        r"""
        ({ remainingCountXPath, insufficientPackHeadingXPathValue }) => {
            const markerPrefix = `auto-wikigacha-count-${Date.now()}-${Math.random().toString(36).slice(2)}`;
            const normalizeWhitespace = (value) => String(value || '').replace(/\s+/g, ' ').trim();
            const normalizeDigits = (value) => String(value || '').replace(/[０-９]/gu, (digit) => {
                return String.fromCharCode(digit.charCodeAt(0) - 0xFF10 + 0x30);
            });
            const insufficientPackPatterns = [
                /卡包不足/iu,
                /pack\s*(?:unavailable|insufficient|depleted|empty|not\s+enough)/iu,
                /(?:unavailable|insufficient|depleted|empty|not\s+enough)\s*pack/iu,
                /パック.*不足|不足.*パック/iu,
            ];
            const progressionCounterPatterns = [
                /次後獲得/iu,
                /次后获得/iu,
                /後獲得/iu,
                /后获得/iu,
                /after\s*\d*\s*(?:more\s*)?(?:draws?|pulls?|opens?)/iu,
                /あと\s*\d*\s*回/iu,
            ];
            const isVisible = (element) => {
                if (!(element instanceof HTMLElement)) {
                    return false;
                }
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && rect.width > 0
                    && rect.height > 0;
            };
            const resolveXPathElement = (xpath) => {
                if (!xpath) {
                    return null;
                }
                try {
                    return document.evaluate(
                        xpath,
                        document,
                        null,
                        XPathResult.FIRST_ORDERED_NODE_TYPE,
                        null,
                    ).singleNodeValue;
                } catch (error) {
                    return null;
                }
            };
            const getElementText = (element) => normalizeWhitespace([
                element.innerText,
                element.textContent,
                element.getAttribute ? element.getAttribute('aria-label') : '',
                element.getAttribute ? element.getAttribute('title') : '',
            ].filter(Boolean).join(' '));
            const parseCountText = (text) => {
                const normalizedText = normalizeDigits(text);
                const numberMatches = Array.from(normalizedText.matchAll(/\d+/gu)).map((match) => Number.parseInt(match[0], 10));
                const finiteNumbers = numberMatches.filter((numberValue) => Number.isFinite(numberValue));
                if (finiteNumbers.length === 0) {
                    return null;
                }
                return {
                    remainingPackCount: finiteNumbers[0],
                    totalPackCapacity: finiteNumbers.length > 1 ? finiteNumbers[1] : null,
                    parsedNumbers: finiteNumbers,
                    normalizedText,
                };
            };
            const buildSyntheticCount = (text, remainingPackCount) => ({
                remainingPackCount,
                totalPackCapacity: null,
                parsedNumbers: [],
                normalizedText: normalizeDigits(text),
            });
            const summarize = (element, source, parsedCount) => {
                const rect = element.getBoundingClientRect();
                return {
                    source,
                    text: getElementText(element).slice(0, 260),
                    normalizedText: parsedCount.normalizedText.slice(0, 260),
                    remainingPackCount: parsedCount.remainingPackCount,
                    totalPackCapacity: parsedCount.totalPackCapacity,
                    parsedNumbers: parsedCount.parsedNumbers,
                    tagName: element.tagName.toLowerCase(),
                    id: element.id || '',
                    className: typeof element.className === 'string' ? element.className.slice(0, 260) : '',
                    area: rect.width * rect.height,
                    top: rect.top,
                    left: rect.left,
                };
            };
            const matchesAnyPattern = (patterns, text) => patterns.some((pattern) => pattern.test(text));

            const configuredInsufficientHeading = resolveXPathElement(insufficientPackHeadingXPathValue);
            const insufficientHeadingCandidates = [
                configuredInsufficientHeading,
                ...Array.from(document.querySelectorAll('h1, h2, h3, [role="heading"]')),
            ]
                .filter((element) => element instanceof HTMLElement)
                .filter(isVisible)
                .filter((element, index, allElements) => index === allElements.findIndex((other) => other === element))
                .map((element) => ({
                    element,
                    text: getElementText(element),
                    source: element === configuredInsufficientHeading ? 'configuredInsufficientPackHeadingXPath' : 'semanticInsufficientPackHeading',
                }))
                .filter((candidate) => matchesAnyPattern(insufficientPackPatterns, candidate.text));

            if (insufficientHeadingCandidates.length > 0) {
                const selectedCandidate = insufficientHeadingCandidates[0];
                const marker = `${markerPrefix}-insufficient-pack`;
                selectedCandidate.element.setAttribute('data-auto-wikigacha-count', marker);
                const syntheticCount = buildSyntheticCount(selectedCandidate.text, 0);
                return {
                    ok: true,
                    selector: `[data-auto-wikigacha-count="${marker}"]`,
                    selected: summarize(selectedCandidate.element, selectedCandidate.source, syntheticCount),
                    candidates: insufficientHeadingCandidates.map((candidate) => {
                        return summarize(candidate.element, candidate.source, buildSyntheticCount(candidate.text, 0));
                    }),
                };
            }

            const configuredElement = resolveXPathElement(remainingCountXPath);
            if (configuredElement instanceof HTMLElement && isVisible(configuredElement)) {
                const configuredText = getElementText(configuredElement);
                const parsedCount = parseCountText(configuredText);
                if (parsedCount && !matchesAnyPattern(progressionCounterPatterns, configuredText)) {
                    const marker = `${markerPrefix}-configured-xpath`;
                    configuredElement.setAttribute('data-auto-wikigacha-count', marker);
                    return {
                        ok: true,
                        selector: `[data-auto-wikigacha-count="${marker}"]`,
                        selected: summarize(configuredElement, 'configuredXPath', parsedCount),
                        candidates: [summarize(configuredElement, 'configuredXPath', parsedCount)],
                    };
                }
            }

            const semanticCandidateSelector = ['span', 'div', 'p', '[aria-label]', '[title]'].join(',');
            const packCounterPatterns = [
                /今日卡包/iu,
                /today.*pack/iu,
                /pack.*(?:remaining|left|count)/iu,
                /(?:remaining|left|count).*pack/iu,
                /本日.*パック|今日.*パック/iu,
                /\//u,
            ];
            const candidates = Array.from(document.querySelectorAll(semanticCandidateSelector))
                .filter((element) => element instanceof HTMLElement)
                .filter(isVisible)
                .map((element, index) => {
                    const text = getElementText(element);
                    const parsedCount = parseCountText(text);
                    const evidence = packCounterPatterns
                        .map((pattern) => pattern.test(text))
                        .filter(Boolean)
                        .length;
                    const progressionEvidence = progressionCounterPatterns
                        .map((pattern) => pattern.test(text))
                        .filter(Boolean)
                        .length;
                    const insufficientEvidence = insufficientPackPatterns
                        .map((pattern) => pattern.test(text))
                        .filter(Boolean)
                        .length;
                    const rect = element.getBoundingClientRect();
                    return {
                        element,
                        marker: `${markerPrefix}-semantic-${index}`,
                        parsedCount,
                        evidence,
                        progressionEvidence,
                        insufficientEvidence,
                        area: rect.width * rect.height,
                        top: rect.top,
                        left: rect.left,
                    };
                })
                .filter((candidate) => candidate.parsedCount && candidate.evidence > 0)
                .filter((candidate) => candidate.progressionEvidence === 0)
                .filter((candidate) => candidate.insufficientEvidence === 0)
                .sort((left, right) => {
                    const comparisons = [
                        right.evidence - left.evidence,
                        left.area - right.area,
                        left.top - right.top,
                        left.left - right.left,
                    ];
                    return comparisons.find((comparison) => comparison !== 0) || 0;
                });

            if (candidates.length === 0) {
                return {
                    ok: false,
                    reason: 'No visible remaining-pack counter was found or parsed.',
                    configuredXPath: remainingCountXPath,
                    configuredXPathVisible: configuredElement instanceof HTMLElement ? isVisible(configuredElement) : false,
                    configuredXPathText: configuredElement instanceof HTMLElement ? getElementText(configuredElement).slice(0, 260) : '',
                    insufficientPackHeadingXPath: insufficientPackHeadingXPathValue,
                    insufficientPackHeadingVisible: configuredInsufficientHeading instanceof HTMLElement ? isVisible(configuredInsufficientHeading) : false,
                    insufficientPackHeadingText: configuredInsufficientHeading instanceof HTMLElement ? getElementText(configuredInsufficientHeading).slice(0, 260) : '',
                    visibleTextSample: document.body ? document.body.innerText.slice(0, 1600) : '',
                };
            }

            const selectedCandidate = candidates[0];
            selectedCandidate.element.setAttribute('data-auto-wikigacha-count', selectedCandidate.marker);
            return {
                ok: true,
                selector: `[data-auto-wikigacha-count="${selectedCandidate.marker}"]`,
                selected: summarize(selectedCandidate.element, 'semanticFallback', selectedCandidate.parsedCount),
                candidates: candidates.map((candidate) => summarize(candidate.element, 'semanticFallback', candidate.parsedCount)),
            };
        }
        """,
        {
            "remainingCountXPath": remainingCountXPath,
            "insufficientPackHeadingXPathValue": insufficientPackHeadingXPathValue,
        },
    )

def getRemainingPackCountValue(remainingPackResolution: dict[str, Any]) -> int | None:
    if not remainingPackResolution.get("ok"):
        return None
    selectedPackCount = remainingPackResolution.get("selected", {}).get("remainingPackCount")
    return selectedPackCount if isinstance(selectedPackCount, int) else None


def hasRemainingPacks(remainingPackResolution: dict[str, Any]) -> bool | None:
    remainingPackCount = getRemainingPackCountValue(remainingPackResolution)
    if remainingPackCount is None:
        return None
    return remainingPackCount > 0

def resolveReturnToPackPageSelector(page: Page, returnButtonXPath: str) -> dict[str, Any]:
    return page.evaluate(
        r"""
        (returnButtonXPath) => {
            const markerPrefix = `auto-wikigacha-return-${Date.now()}-${Math.random().toString(36).slice(2)}`;
            const semanticCandidateSelector = [
                'button',
                'a[href]',
                '[role="button"]',
                '[onclick]',
                '[tabindex]',
                '.cursor-pointer'
            ].join(',');
            const returnPatterns = [
                /返回卡包頁面/iu,
                /返回卡包页面/iu,
                /回到卡包/iu,
                /回卡包/iu,
                /返回.*卡包/iu,
                /卡包.*返回/iu,
                /return\s+to\s+pack/iu,
                /back\s+to\s+pack/iu,
                /pack\s+page/iu,
                /パック.*戻|戻.*パック/iu
            ];
            const resultUtilityPatterns = [
                /複製結果|复制结果|分享結果|分享结果/iu,
                /copy\s+result|share\s+result/iu,
                /分享|share|シェア/iu,
                /複製|复制|copy/iu,
                /結果|结果|result/iu,
            ];
            const navigationNegativePatterns = [
                /圖鑑|图鉴|図鑑/iu,
                /對戰|对战|battle|バトル/iu,
                /獎盃|奖杯|trophy/iu,
                /遊戲說明|游戏说明|help|rule/iu,
                /privacy|policy|terms|contact/iu,
                /隱私|隐私|條款|条款|聯絡|联系|お問い合わせ/iu,
            ];
            const getClassText = (element) => typeof element.className === 'string' ? element.className : '';
            const normalizeWhitespace = (value) => String(value || '').replace(/\s+/g, ' ').trim();
            const getNormalizedText = (element) => {
                if (!element) {
                    return '';
                }
                return normalizeWhitespace([
                    element.innerText,
                    element.textContent,
                    element.getAttribute ? element.getAttribute('aria-label') : '',
                    element.getAttribute ? element.getAttribute('title') : '',
                    element.getAttribute ? element.getAttribute('value') : '',
                    element.id,
                    getClassText(element),
                    ...Array.from(element.querySelectorAll ? element.querySelectorAll('img[alt]') : [])
                        .map((image) => image.getAttribute('alt'))
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
                const rectangles = Array.from(element.getClientRects()).filter((rect) => rect.width > 0 && rect.height > 0);
                const targetRectangles = rectangles.length > 0 ? rectangles : [element.getBoundingClientRect()];
                return targetRectangles.some((rect) => {
                    const centerX = rect.left + rect.width / 2;
                    const centerY = rect.top + rect.height / 2;
                    const hitElement = document.elementFromPoint(centerX, centerY);
                    return Boolean(hitElement && (element === hitElement || element.contains(hitElement) || hitElement.contains(element)));
                });
            };
            const isActionable = (element) => {
                if (!(element instanceof HTMLElement)) {
                    return false;
                }
                const style = window.getComputedStyle(element);
                const tagName = element.tagName.toLowerCase();
                return tagName === 'button'
                    || tagName === 'a'
                    || element.getAttribute('role') === 'button'
                    || element.hasAttribute('onclick')
                    || element.hasAttribute('tabindex')
                    || style.cursor === 'pointer';
            };
            const resolveXPathElement = (xpath) => {
                if (!xpath) {
                    return null;
                }
                try {
                    return document.evaluate(
                        xpath,
                        document,
                        null,
                        XPathResult.FIRST_ORDERED_NODE_TYPE,
                        null,
                    ).singleNodeValue;
                } catch (error) {
                    return null;
                }
            };
            const getEvidenceCount = (patterns, text) => patterns
                .map((pattern) => pattern.test(text))
                .filter(Boolean)
                .length;
            const summarize = (element, source, evidence) => {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return {
                    text: getNormalizedText(element).slice(0, 260),
                    tagName: element.tagName.toLowerCase(),
                    role: element.getAttribute('role') || '',
                    href: element.getAttribute('href') || '',
                    id: element.id || '',
                    className: getClassText(element).slice(0, 260),
                    source,
                    returnEvidence: evidence.returnEvidence,
                    resultUtilityEvidence: evidence.resultUtilityEvidence,
                    navigationNegativeEvidence: evidence.navigationNegativeEvidence,
                    pointerReceivable: isPointerReceivable(element),
                    actionable: isActionable(element),
                    cursor: style.cursor,
                    area: rect.width * rect.height,
                    top: rect.top,
                    left: rect.left,
                };
            };
            const buildCandidate = (element, marker, source) => {
                const text = getNormalizedText(element);
                const evidence = {
                    returnEvidence: getEvidenceCount(returnPatterns, text),
                    resultUtilityEvidence: getEvidenceCount(resultUtilityPatterns, text),
                    navigationNegativeEvidence: getEvidenceCount(navigationNegativePatterns, text),
                };
                return {
                    element,
                    marker,
                    source,
                    text,
                    ...evidence,
                    summary: summarize(element, source, evidence),
                };
            };

            const configuredElement = resolveXPathElement(returnButtonXPath);
            const configuredCandidate = configuredElement instanceof HTMLElement
                ? buildCandidate(configuredElement, `${markerPrefix}-configured-xpath`, 'configuredXPath')
                : null;

            const semanticCandidates = Array.from(document.querySelectorAll(semanticCandidateSelector))
                .filter((element) => element instanceof HTMLElement)
                .map((element, index) => buildCandidate(element, `${markerPrefix}-semantic-${index}`, 'semanticFallback'));

            const candidates = [configuredCandidate, ...semanticCandidates]
                .filter(Boolean)
                .filter((candidate, index, allCandidates) => {
                    return index === allCandidates.findIndex((other) => other.element === candidate.element);
                })
                .filter((candidate) => isVisible(candidate.element))
                .filter((candidate) => candidate.summary.actionable)
                .filter((candidate) => candidate.returnEvidence > 0)
                .filter((candidate) => candidate.navigationNegativeEvidence === 0)
                .sort((left, right) => {
                    const comparisons = [
                        right.returnEvidence - left.returnEvidence,
                        left.resultUtilityEvidence - right.resultUtilityEvidence,
                        left.navigationNegativeEvidence - right.navigationNegativeEvidence,
                        Number(right.summary.pointerReceivable) - Number(left.summary.pointerReceivable),
                        right.summary.area - left.summary.area,
                        right.summary.top - left.summary.top,
                        left.summary.left - right.summary.left,
                    ];
                    return comparisons.find((comparison) => comparison !== 0) || 0;
                });

            const rejectedConfiguredElement = configuredCandidate
                ? configuredCandidate.summary
                : null;

            if (candidates.length === 0) {
                return {
                    ok: false,
                    reason: 'No visible semantic return-to-pack-page button was found. Configured XPath is not trusted unless its own text matches a return-to-pack action.',
                    configuredXPath: returnButtonXPath,
                    configuredXPathResolved: configuredElement instanceof HTMLElement,
                    rejectedConfiguredElement,
                    candidates: [configuredCandidate, ...semanticCandidates]
                        .filter(Boolean)
                        .map((candidate) => candidate.summary)
                        .slice(0, 48),
                    visibleTextSample: document.body ? document.body.innerText.slice(0, 1600) : '',
                };
            }

            const selectedCandidate = candidates[0];
            selectedCandidate.element.setAttribute('data-auto-wikigacha-return', selectedCandidate.marker);
            return {
                ok: true,
                selector: `[data-auto-wikigacha-return="${selectedCandidate.marker}"]`,
                selected: selectedCandidate.summary,
                rejectedConfiguredElement,
                candidates: candidates.map((candidate) => candidate.summary),
            };
        }
        """,
        returnButtonXPath,
    )

def buildResolutionFingerprint(resolution: dict[str, Any]) -> str:
    comparableResolution = {
        "ok": resolution.get("ok"),
        "reason": resolution.get("reason"),
        "selected": resolution.get("selected"),
    }
    return buildShortHash(json.dumps(comparableResolution, ensure_ascii=False, sort_keys=True))


def clickSelectorUsingFreshDomElement(page: Page, selector: str) -> dict[str, Any]:
    return page.evaluate(
        r"""
        (selector) => new Promise((resolve) => {
            const normalizeWhitespace = (value) => String(value || '').replace(/\s+/g, ' ').trim();
            const summarizeElement = (element) => {
                if (!(element instanceof HTMLElement)) {
                    return null;
                }
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return {
                    tagName: element.tagName.toLowerCase(),
                    id: element.id || '',
                    className: typeof element.className === 'string' ? element.className.slice(0, 260) : '',
                    role: element.getAttribute('role') || '',
                    text: normalizeWhitespace([
                        element.innerText,
                        element.textContent,
                        element.getAttribute('aria-label'),
                        element.getAttribute('title'),
                        element.getAttribute('value'),
                    ].filter(Boolean).join(' ')).slice(0, 260),
                    connected: element.isConnected,
                    rect: {
                        left: rect.left,
                        top: rect.top,
                        right: rect.right,
                        bottom: rect.bottom,
                        width: rect.width,
                        height: rect.height,
                    },
                    display: style.display,
                    visibility: style.visibility,
                    opacity: style.opacity,
                    pointerEvents: style.pointerEvents,
                };
            };
            const isUsableElement = (element) => {
                if (!(element instanceof HTMLElement)) {
                    return { ok: false, reason: 'selectorDidNotResolveToHTMLElement' };
                }
                if (!element.isConnected || !document.documentElement.contains(element)) {
                    return { ok: false, reason: 'elementIsDetachedFromDom' };
                }
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                if (style.visibility === 'hidden' || style.display === 'none' || rect.width <= 0 || rect.height <= 0) {
                    return { ok: false, reason: 'elementIsNotRendered' };
                }
                if (element.hasAttribute('disabled') || element.getAttribute('aria-disabled') === 'true') {
                    return { ok: false, reason: 'elementIsDisabled' };
                }
                return { ok: true, reason: 'elementIsFreshAndUsable' };
            };
            const getPointerReceivable = (element) => {
                const rectangles = Array.from(element.getClientRects()).filter((rect) => rect.width > 0 && rect.height > 0);
                const targetRectangles = rectangles.length > 0 ? rectangles : [element.getBoundingClientRect()];
                return targetRectangles.some((rect) => {
                    const centerX = rect.left + rect.width / 2;
                    const centerY = rect.top + rect.height / 2;
                    const hitElement = document.elementFromPoint(centerX, centerY);
                    return Boolean(hitElement && (element === hitElement || element.contains(hitElement) || hitElement.contains(element)));
                });
            };
            const resolveCurrentElement = () => document.querySelector(selector);
            const initialElement = resolveCurrentElement();
            const initialValidation = isUsableElement(initialElement);
            if (!initialValidation.ok) {
                resolve({
                    ok: false,
                    reason: initialValidation.reason,
                    selector,
                    selectorRefreshRecommended: true,
                    elementSummary: summarizeElement(initialElement),
                });
                return;
            }

            window.requestAnimationFrame(() => {
                const freshElement = resolveCurrentElement();
                const freshValidation = isUsableElement(freshElement);
                if (!freshValidation.ok) {
                    resolve({
                        ok: false,
                        reason: freshValidation.reason,
                        selector,
                        selectorRefreshRecommended: true,
                        elementSummary: summarizeElement(freshElement),
                    });
                    return;
                }

                const pointerReceivable = getPointerReceivable(freshElement);
                try {
                    freshElement.click();
                    resolve({
                        ok: true,
                        reason: 'clickedFreshDomElement',
                        selector,
                        clickMethod: 'HTMLElement.click',
                        pointerReceivable,
                        elementSummary: summarizeElement(freshElement),
                    });
                } catch (error) {
                    resolve({
                        ok: false,
                        reason: 'domClickRaisedException',
                        selector,
                        selectorRefreshRecommended: true,
                        errorName: error && error.name ? error.name : '',
                        errorMessage: error && error.message ? error.message : String(error),
                        pointerReceivable,
                        elementSummary: summarizeElement(freshElement),
                    });
                }
            });
        })
        """,
        selector,
    )


def clickResolvedSelectorAndWait(
    page: Page,
    selector: str,
    refreshResolution: Callable[[], dict[str, Any]] | None = None,
    returnOnRefreshResolutionFailure: bool = False,
) -> dict[str, Any]:
    previousFingerprint = getPageFingerprint(page)
    previousRenderedStateFingerprint = getRenderedStateFingerprint(page)
    currentSelector = selector
    clickAttempts: list[dict[str, Any]] = []
    refreshedResolutions: list[dict[str, Any]] = []
    seenRefreshedResolutionFingerprints: set[str] = set()

    def buildClickOutcome(
        clickCompleted: bool,
        clickAbortedReason: str | None = None,
        refreshResolutionFailure: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        renderObservation = waitForRenderCycle(page)
        currentFingerprint = getPageFingerprint(page)
        currentRenderedStateFingerprint = getRenderedStateFingerprint(page)
        pageFingerprintChanged = previousFingerprint != currentFingerprint
        renderedStateChanged = previousRenderedStateFingerprint != currentRenderedStateFingerprint
        stateChanged = pageFingerprintChanged or renderedStateChanged
        return {
            "previousFingerprintHash": buildShortHash(previousFingerprint),
            "currentFingerprintHash": buildShortHash(currentFingerprint),
            "previousRenderedStateFingerprintHash": buildShortHash(previousRenderedStateFingerprint),
            "currentRenderedStateFingerprintHash": buildShortHash(currentRenderedStateFingerprint),
            "pageFingerprintChanged": pageFingerprintChanged,
            "renderedStateChanged": renderedStateChanged,
            "stateChanged": stateChanged,
            "fingerprintChanged": stateChanged,
            "clickCompleted": clickCompleted,
            "clickAborted": not clickCompleted,
            "clickAbortedReason": clickAbortedReason,
            "refreshResolutionFailure": refreshResolutionFailure,
            "initialSelector": selector,
            "finalSelector": currentSelector,
            "clickAttempts": clickAttempts,
            "refreshedResolutions": refreshedResolutions,
            "renderObservation": renderObservation,
        }

    while True:
        clickAttempt = clickSelectorUsingFreshDomElement(page, currentSelector)
        clickAttempts.append(clickAttempt)
        if clickAttempt.get("ok"):
            break

        if refreshResolution is None or not clickAttempt.get("selectorRefreshRecommended"):
            if returnOnRefreshResolutionFailure:
                return buildClickOutcome(
                    clickCompleted=False,
                    clickAbortedReason="freshDomClickFailedWithoutRefreshPath",
                )
            raise WikiGachaAutomationError(
                "Resolved selector could not be clicked because the target element was no longer fresh in the DOM. "
                "Inspect the clickAttempts payload for the upstream stale-element cause."
            )

        refreshedResolution = refreshResolution()
        refreshedResolutions.append(refreshedResolution)
        if not refreshedResolution.get("ok"):
            if returnOnRefreshResolutionFailure:
                return buildClickOutcome(
                    clickCompleted=False,
                    clickAbortedReason="refreshResolutionReturnedNoFreshTarget",
                    refreshResolutionFailure=refreshedResolution,
                )
            raise WikiGachaAutomationError(
                refreshedResolution.get(
                    "reason",
                    "Resolved selector became stale and no fresh replacement target could be resolved.",
                )
            )

        refreshedResolutionFingerprint = buildResolutionFingerprint(refreshedResolution)
        if refreshedResolutionFingerprint in seenRefreshedResolutionFingerprints:
            if returnOnRefreshResolutionFailure:
                return buildClickOutcome(
                    clickCompleted=False,
                    clickAbortedReason="refreshedResolutionRepeatedBeforeClick",
                    refreshResolutionFailure=refreshedResolution,
                )
            raise WikiGachaAutomationError(
                "Resolved selector repeatedly became stale after semantic refresh; the page is replacing the same target "
                "before it can be clicked. Inspect refreshedResolutions and clickAttempts evidence."
            )
        seenRefreshedResolutionFingerprints.add(refreshedResolutionFingerprint)
        currentSelector = refreshedResolution["selector"]

    return buildClickOutcome(clickCompleted=True)


def resolveInsufficientPackRecoverySelector(
    page: Page,
    insufficientPackHeadingXPathValue: str,
    recoverPackButtonXPathValue: str,
) -> dict[str, Any]:
    return page.evaluate(
        r"""
        ({ insufficientPackHeadingXPathValue, recoverPackButtonXPathValue }) => {
            const markerPrefix = `auto-wikigacha-insufficient-${Date.now()}-${Math.random().toString(36).slice(2)}`;
            const actionableSelector = [
                'button',
                'a[href]',
                '[role="button"]',
                '[onclick]',
                '[tabindex]'
            ].join(',');
            const insufficientPackPatterns = [
                /卡包不足/iu,
                /pack\s*(?:unavailable|insufficient|depleted|empty|not\s+enough)/iu,
                /(?:unavailable|insufficient|depleted|empty|not\s+enough)\s*pack/iu,
                /パック.*不足|不足.*パック/iu,
            ];
            const recoveryActionPatterns = [
                /觀看廣告恢復/iu,
                /观看广告恢复/iu,
                /廣告.*恢復|恢復.*廣告/iu,
                /广告.*恢复|恢复.*广告/iu,
                /watch.*ad.*(?:recover|restore|refill|reward)/iu,
                /(?:recover|restore|refill|reward).*watch.*ad/iu,
                /広告.*回復|回復.*広告/iu,
            ];
            const negativeActionPatterns = [
                /圖鑑|图鉴|図鑑/iu,
                /對戰|对战|battle|バトル/iu,
                /獎盃|奖杯|trophy/iu,
                /遊戲說明|游戏说明|help|rule/iu,
                /privacy|policy|terms|contact/iu,
                /隱私|隐私|條款|条款|聯絡|联系/iu,
            ];
            const normalizeWhitespace = (value) => String(value || '').replace(/\s+/g, ' ').trim();
            const getClassText = (element) => typeof element.className === 'string' ? element.className : '';
            const getElementText = (element) => {
                if (!element) {
                    return '';
                }
                return normalizeWhitespace([
                    element.innerText,
                    element.textContent,
                    element.getAttribute ? element.getAttribute('aria-label') : '',
                    element.getAttribute ? element.getAttribute('title') : '',
                    element.getAttribute ? element.getAttribute('value') : '',
                    element.id,
                    getClassText(element),
                    ...Array.from(element.querySelectorAll ? element.querySelectorAll('img[alt]') : [])
                        .map((image) => image.getAttribute('alt')),
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
            const resolveXPathElement = (xpath) => {
                if (!xpath) {
                    return null;
                }
                try {
                    return document.evaluate(
                        xpath,
                        document,
                        null,
                        XPathResult.FIRST_ORDERED_NODE_TYPE,
                        null,
                    ).singleNodeValue;
                } catch (error) {
                    return null;
                }
            };
            const getEvidenceCount = (patterns, text) => patterns
                .map((pattern) => pattern.test(text))
                .filter(Boolean)
                .length;
            const summarizeAction = (element, source, heading) => {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return {
                    source,
                    text: getElementText(element).slice(0, 260),
                    headingText: heading ? getElementText(heading).slice(0, 260) : '',
                    tagName: element.tagName.toLowerCase(),
                    role: element.getAttribute('role') || '',
                    id: element.id || '',
                    className: getClassText(element).slice(0, 260),
                    pointerReceivable: isPointerReceivable(element),
                    cursor: style.cursor,
                    area: rect.width * rect.height,
                    top: rect.top,
                    left: rect.left,
                };
            };

            const configuredHeading = resolveXPathElement(insufficientPackHeadingXPathValue);
            const headingCandidates = [
                configuredHeading,
                ...Array.from(document.querySelectorAll('h1, h2, h3, [role="heading"]')),
            ]
                .filter((element) => element instanceof HTMLElement)
                .filter(isVisible)
                .filter((element, index, allElements) => index === allElements.findIndex((other) => other === element))
                .filter((element) => getEvidenceCount(insufficientPackPatterns, getElementText(element)) > 0);

            if (headingCandidates.length === 0) {
                return {
                    ok: false,
                    reason: 'No visible insufficient-pack heading was detected.',
                    insufficientPackHeadingXPath: insufficientPackHeadingXPathValue,
                    configuredHeadingVisible: configuredHeading instanceof HTMLElement ? isVisible(configuredHeading) : false,
                    configuredHeadingText: configuredHeading instanceof HTMLElement ? getElementText(configuredHeading).slice(0, 260) : '',
                    visibleTextSample: document.body ? document.body.innerText.slice(0, 1600) : '',
                };
            }

            const selectedHeading = headingCandidates[0];
            const configuredButton = resolveXPathElement(recoverPackButtonXPathValue);
            const configuredButtonIsValid = configuredButton instanceof HTMLElement
                && isVisible(configuredButton)
                && isPointerReceivable(configuredButton)
                && getEvidenceCount(negativeActionPatterns, getElementText(configuredButton)) === 0;
            if (configuredButtonIsValid) {
                const marker = `${markerPrefix}-configured-recovery-button`;
                configuredButton.setAttribute('data-auto-wikigacha-insufficient', marker);
                return {
                    ok: true,
                    selector: `[data-auto-wikigacha-insufficient="${marker}"]`,
                    selected: summarizeAction(configuredButton, 'configuredRecoverPackButtonXPath', selectedHeading),
                    candidates: [summarizeAction(configuredButton, 'configuredRecoverPackButtonXPath', selectedHeading)],
                };
            }

            const candidates = Array.from(document.querySelectorAll(actionableSelector))
                .filter((element) => element instanceof HTMLElement)
                .filter(isVisible)
                .filter(isPointerReceivable)
                .map((element, index) => {
                    const text = getElementText(element);
                    const rect = element.getBoundingClientRect();
                    return {
                        element,
                        marker: `${markerPrefix}-semantic-${index}`,
                        recoveryEvidence: getEvidenceCount(recoveryActionPatterns, text),
                        negativeEvidence: getEvidenceCount(negativeActionPatterns, text),
                        area: rect.width * rect.height,
                        top: rect.top,
                        left: rect.left,
                    };
                })
                .filter((candidate) => candidate.recoveryEvidence > 0)
                .filter((candidate) => candidate.negativeEvidence === 0)
                .sort((left, right) => {
                    const comparisons = [
                        right.recoveryEvidence - left.recoveryEvidence,
                        right.area - left.area,
                        left.top - right.top,
                        left.left - right.left,
                    ];
                    return comparisons.find((comparison) => comparison !== 0) || 0;
                });

            if (candidates.length === 0) {
                return {
                    ok: false,
                    reason: 'Insufficient-pack state was detected, but no recovery action button was resolved.',
                    insufficientPackHeadingXPath: insufficientPackHeadingXPathValue,
                    recoverPackButtonXPath: recoverPackButtonXPathValue,
                    configuredButtonVisible: configuredButton instanceof HTMLElement ? isVisible(configuredButton) : false,
                    configuredButtonPointerReceivable: configuredButton instanceof HTMLElement ? isPointerReceivable(configuredButton) : false,
                    headingText: getElementText(selectedHeading).slice(0, 260),
                    visibleTextSample: document.body ? document.body.innerText.slice(0, 1600) : '',
                };
            }

            const selectedCandidate = candidates[0];
            selectedCandidate.element.setAttribute('data-auto-wikigacha-insufficient', selectedCandidate.marker);
            return {
                ok: true,
                selector: `[data-auto-wikigacha-insufficient="${selectedCandidate.marker}"]`,
                selected: summarizeAction(selectedCandidate.element, 'semanticRecoveryAction', selectedHeading),
                candidates: candidates.map((candidate) => summarizeAction(candidate.element, 'semanticRecoveryAction', selectedHeading)),
            };
        }
        """,
        {
            "insufficientPackHeadingXPathValue": insufficientPackHeadingXPathValue,
            "recoverPackButtonXPathValue": recoverPackButtonXPathValue,
        },
    )


def resolveAdRewardConfirmationSelector(page: Page, adRewardConfirmButtonXPathValue: str) -> dict[str, Any]:
    return page.evaluate(
        r"""
        (adRewardConfirmButtonXPathValue) => {
            const markerPrefix = `auto-wikigacha-ad-confirm-${Date.now()}-${Math.random().toString(36).slice(2)}`;
            const actionableSelector = [
                'button',
                'a[href]',
                '[role="button"]',
                '[onclick]',
                '[tabindex]'
            ].join(',');
            const confirmationPatterns = [
                /^(?:確定|確認|關閉|關掉|領取|獲得|取得|完成|繼續|開始|好|OK)$/iu,
                /^(?:确定|确认|关闭|领取|获得|取得|完成|继续|开始|好|OK)$/iu,
                /(?:claim|collect|get|receive|close|continue|done|confirm|ok|reward)/iu,
                /^(?:閉じる|確認|受け取る|獲得|取得|完了|続ける|OK)$/iu,
            ];
            const negativePatterns = [
                /圖鑑|图鉴|図鑑/iu,
                /對戰|对战|battle|バトル/iu,
                /獎盃|奖杯|trophy/iu,
                /遊戲說明|游戏说明|help|rule/iu,
                /privacy|policy|terms|contact/iu,
                /隱私|隐私|條款|条款|聯絡|联系/iu,
            ];
            const normalizeWhitespace = (value) => String(value || '').replace(/\s+/g, ' ').trim();
            const getClassText = (element) => typeof element.className === 'string' ? element.className : '';
            const getElementText = (element) => {
                if (!element) {
                    return '';
                }
                return normalizeWhitespace([
                    element.innerText,
                    element.textContent,
                    element.getAttribute ? element.getAttribute('aria-label') : '',
                    element.getAttribute ? element.getAttribute('title') : '',
                    element.getAttribute ? element.getAttribute('value') : '',
                    element.id,
                    getClassText(element),
                    ...Array.from(element.querySelectorAll ? element.querySelectorAll('img[alt]') : [])
                        .map((image) => image.getAttribute('alt')),
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
            const resolveXPathElement = (xpath) => {
                if (!xpath) {
                    return null;
                }
                try {
                    return document.evaluate(
                        xpath,
                        document,
                        null,
                        XPathResult.FIRST_ORDERED_NODE_TYPE,
                        null,
                    ).singleNodeValue;
                } catch (error) {
                    return null;
                }
            };
            const getEvidenceCount = (patterns, text) => patterns
                .map((pattern) => pattern.test(text))
                .filter(Boolean)
                .length;
            const summarizeAction = (element, source) => {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return {
                    source,
                    text: getElementText(element).slice(0, 260),
                    tagName: element.tagName.toLowerCase(),
                    role: element.getAttribute('role') || '',
                    id: element.id || '',
                    className: getClassText(element).slice(0, 260),
                    pointerReceivable: isPointerReceivable(element),
                    cursor: style.cursor,
                    area: rect.width * rect.height,
                    top: rect.top,
                    left: rect.left,
                };
            };

            const configuredButton = resolveXPathElement(adRewardConfirmButtonXPathValue);
            if (configuredButton instanceof HTMLElement && isVisible(configuredButton) && isPointerReceivable(configuredButton)) {
                const marker = `${markerPrefix}-configured-xpath`;
                configuredButton.setAttribute('data-auto-wikigacha-ad-confirm', marker);
                return {
                    ok: true,
                    selector: `[data-auto-wikigacha-ad-confirm="${marker}"]`,
                    selected: summarizeAction(configuredButton, 'configuredAdRewardConfirmButtonXPath'),
                    candidates: [summarizeAction(configuredButton, 'configuredAdRewardConfirmButtonXPath')],
                };
            }

            const candidates = Array.from(document.querySelectorAll(actionableSelector))
                .filter((element) => element instanceof HTMLElement)
                .filter(isVisible)
                .filter(isPointerReceivable)
                .map((element, index) => {
                    const text = getElementText(element);
                    const rect = element.getBoundingClientRect();
                    return {
                        element,
                        marker: `${markerPrefix}-semantic-${index}`,
                        confirmationEvidence: getEvidenceCount(confirmationPatterns, text),
                        negativeEvidence: getEvidenceCount(negativePatterns, text),
                        area: rect.width * rect.height,
                        top: rect.top,
                        left: rect.left,
                    };
                })
                .filter((candidate) => candidate.confirmationEvidence > 0)
                .filter((candidate) => candidate.negativeEvidence === 0)
                .sort((left, right) => {
                    const comparisons = [
                        right.confirmationEvidence - left.confirmationEvidence,
                        right.area - left.area,
                        right.top - left.top,
                        left.left - right.left,
                    ];
                    return comparisons.find((comparison) => comparison !== 0) || 0;
                });

            if (candidates.length === 0) {
                return {
                    ok: false,
                    reason: 'No visible ad-reward confirmation button was resolved.',
                    adRewardConfirmButtonXPath: adRewardConfirmButtonXPathValue,
                    configuredButtonVisible: configuredButton instanceof HTMLElement ? isVisible(configuredButton) : false,
                    configuredButtonPointerReceivable: configuredButton instanceof HTMLElement ? isPointerReceivable(configuredButton) : false,
                    visibleTextSample: document.body ? document.body.innerText.slice(0, 1600) : '',
                };
            }

            const selectedCandidate = candidates[0];
            selectedCandidate.element.setAttribute('data-auto-wikigacha-ad-confirm', selectedCandidate.marker);
            return {
                ok: true,
                selector: `[data-auto-wikigacha-ad-confirm="${selectedCandidate.marker}"]`,
                selected: summarizeAction(selectedCandidate.element, 'semanticAdRewardConfirmationAction'),
                candidates: candidates.map((candidate) => summarizeAction(candidate.element, 'semanticAdRewardConfirmationAction')),
            };
        }
        """,
        adRewardConfirmButtonXPathValue,
    )


def waitForAdRewardConfirmationTarget(page: Page, adRewardConfirmButtonXPathValue: str) -> None:
    page.wait_for_function(
        r"""
        (adRewardConfirmButtonXPathValue) => {
            const actionableSelector = [
                'button',
                'a[href]',
                '[role="button"]',
                '[onclick]',
                '[tabindex]'
            ].join(',');
            const confirmationPatterns = [
                /^(?:確定|確認|關閉|關掉|領取|獲得|取得|完成|繼續|開始|好|OK)$/iu,
                /^(?:确定|确认|关闭|领取|获得|取得|完成|继续|开始|好|OK)$/iu,
                /(?:claim|collect|get|receive|close|continue|done|confirm|ok|reward)/iu,
                /^(?:閉じる|確認|受け取る|獲得|取得|完了|続ける|OK)$/iu,
            ];
            const negativePatterns = [
                /圖鑑|图鉴|図鑑/iu,
                /對戰|对战|battle|バトル/iu,
                /獎盃|奖杯|trophy/iu,
                /遊戲說明|游戏说明|help|rule/iu,
                /privacy|policy|terms|contact/iu,
                /隱私|隐私|條款|条款|聯絡|联系/iu,
            ];
            const normalizeWhitespace = (value) => String(value || '').replace(/\s+/g, ' ').trim();
            const getClassText = (element) => typeof element.className === 'string' ? element.className : '';
            const getElementText = (element) => {
                if (!element) {
                    return '';
                }
                return normalizeWhitespace([
                    element.innerText,
                    element.textContent,
                    element.getAttribute ? element.getAttribute('aria-label') : '',
                    element.getAttribute ? element.getAttribute('title') : '',
                    element.getAttribute ? element.getAttribute('value') : '',
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
            const resolveXPathElement = (xpath) => {
                if (!xpath) {
                    return null;
                }
                try {
                    return document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                } catch (error) {
                    return null;
                }
            };
            const configuredButton = resolveXPathElement(adRewardConfirmButtonXPathValue);
            if (configuredButton instanceof HTMLElement && isVisible(configuredButton) && isPointerReceivable(configuredButton)) {
                return true;
            }
            return Array.from(document.querySelectorAll(actionableSelector))
                .filter((element) => element instanceof HTMLElement)
                .filter(isVisible)
                .filter(isPointerReceivable)
                .some((element) => {
                    const text = getElementText(element);
                    return confirmationPatterns.some((pattern) => pattern.test(text))
                        && !negativePatterns.some((pattern) => pattern.test(text));
                });
        }
        """,
        arg=adRewardConfirmButtonXPathValue,
        timeout=0,
    )


def resolveAdInterruptionCloseSelector(page: Page, adOverlayCloseButtonXPathValue: str) -> dict[str, Any]:
    return page.evaluate(
        r"""
        (adOverlayCloseButtonXPathValue) => {
            const markerPrefix = `auto-wikigacha-ad-close-${Date.now()}-${Math.random().toString(36).slice(2)}`;
            const actionableSelector = [
                '#reward_close_button_widget #close_button',
                '#reward_close_button_widget [role="button"]',
                '#close_button[aria-label]',
                '#close_button[role="button"]',
                '#close_button[tabindex]',
                '#close_button',
                '#close_button_icon',
                '[aria-label="關閉影片"]',
                '[aria-label="关闭影片"]',
                '[aria-label*="關閉影片"]',
                '[aria-label*="关闭影片"]',
                '[aria-label*="關閉廣告"]',
                '[aria-label*="关闭广告"]',
                '[aria-label*="close video" i]',
                '[aria-label*="close ad" i]',
                'button',
                'a[href]',
                '[role="button"]',
                '[onclick]',
                '[tabindex]'
            ].join(',');
            const adInterruptionPatterns = [
                /贊助鏈接|赞助链接|sponsored\s*link/iu,
                /Monetag/iu,
                /Google\s*AI\s*Plus|one\.google\.com/iu,
                /reward_close_button_widget|close_button_icon|close_button/iu,
                /廣告已暫時停用|广告已暂时停用/iu,
                /廣告.*暫時.*停用|广告.*暂时.*停用/iu,
                /ad(?:vertisement)?\s*(?:temporarily\s*)?(?:disabled|unavailable|paused|suspended)/iu,
                /請稍候|请稍候|please\s*wait/iu,
                /#goog_rewarded|goog_rewarded|rewarded\s*ad|google\s*rewarded/iu,
            ];
            const closeActionPatterns = [
                /^(?:關閉影片|关闭影片|關閉廣告|关闭广告|關閉|关闭|關掉|關閉視窗|关闭窗口)$/iu,
                /^(?:close\s*(?:video|ad|advertisement)?|dismiss)$/iu,
                /^(?:広告を閉じる|閉じる)$/iu,
                /(?:關閉|关闭|close|dismiss).*(?:影片|廣告|广告|video|ad|advertisement)/iu,
                /(?:影片|廣告|广告|video|ad|advertisement).*(?:關閉|关闭|close|dismiss)/iu,
                /^close_button(?:_icon)?$/iu,
                /reward_close_button_widget/iu,
                /^\s*[×✕✖x]\s*$/iu,
            ];
            const negativeActionPatterns = [
                /圖鑑|图鉴|図鑑/iu,
                /對戰|对战|battle|バトル/iu,
                /獎盃|奖杯|trophy/iu,
                /遊戲說明|游戏说明|help|rule/iu,
                /privacy|policy|terms|contact/iu,
                /隱私|隐私|條款|条款|聯絡|联系/iu,
                /瞭解詳情|了解详情|learn\s*more|details/iu,
            ];
            const normalizeWhitespace = (value) => String(value || '').replace(/\s+/g, ' ').trim();
            const getClassText = (element) => typeof element.className === 'string' ? element.className : '';
            const getElementText = (element) => {
                if (!element) {
                    return '';
                }
                return normalizeWhitespace([
                    element.innerText,
                    element.textContent,
                    element.getAttribute ? element.getAttribute('aria-label') : '',
                    element.getAttribute ? element.getAttribute('title') : '',
                    element.getAttribute ? element.getAttribute('value') : '',
                    element.id,
                    getClassText(element),
                    ...Array.from(element.querySelectorAll ? element.querySelectorAll('img[alt]') : [])
                        .map((image) => image.getAttribute('alt')),
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
                const rectangles = Array.from(element.getClientRects()).filter((rect) => rect.width > 0 && rect.height > 0);
                const targetRectangles = rectangles.length > 0 ? rectangles : [element.getBoundingClientRect()];
                return targetRectangles.some((rect) => {
                    const centerX = rect.left + rect.width / 2;
                    const centerY = rect.top + rect.height / 2;
                    const hitElement = document.elementFromPoint(centerX, centerY);
                    return Boolean(hitElement && (element === hitElement || element.contains(hitElement) || hitElement.contains(element)));
                });
            };
            const resolveXPathElement = (xpath) => {
                if (!xpath) {
                    return null;
                }
                try {
                    return document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                } catch (error) {
                    return null;
                }
            };
            const getEvidenceCount = (patterns, text) => patterns
                .map((pattern) => pattern.test(text))
                .filter(Boolean)
                .length;
            const buildRootSummary = () => {
                const bodyText = getElementText(document.body);
                return {
                    text: bodyText,
                    adInterruptionEvidence: getEvidenceCount(adInterruptionPatterns, `${location.href} ${bodyText}`),
                    closeActionEvidence: 0,
                    source: 'pageBody',
                };
            };
            const findAdInterruptionRoot = () => {
                const visibleElements = Array.from(document.querySelectorAll('body *'))
                    .filter((element) => element instanceof HTMLElement)
                    .filter(isVisible);
                const roots = visibleElements
                    .map((element) => {
                        const text = getElementText(element);
                        const adInterruptionEvidence = getEvidenceCount(adInterruptionPatterns, `${location.href} ${text}`);
                        const closeActionEvidence = Array.from(element.querySelectorAll(actionableSelector))
                            .filter((action) => action instanceof HTMLElement)
                            .filter(isVisible)
                            .map((action) => getEvidenceCount(closeActionPatterns, getElementText(action)))
                            .reduce((total, evidence) => total + evidence, 0);
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return {
                            element,
                            text,
                            adInterruptionEvidence,
                            closeActionEvidence,
                            area: rect.width * rect.height,
                            top: rect.top,
                            left: rect.left,
                            position: style.position,
                            zIndex: style.zIndex,
                            source: 'semanticAdInterruptionRoot',
                        };
                    })
                    .filter((candidate) => candidate.adInterruptionEvidence > 0)
                    .sort((left, right) => {
                        const comparisons = [
                            right.adInterruptionEvidence - left.adInterruptionEvidence,
                            right.closeActionEvidence - left.closeActionEvidence,
                            left.area - right.area,
                            right.top - left.top,
                            left.left - right.left,
                        ];
                        return comparisons.find((comparison) => comparison !== 0) || 0;
                    });
                return roots.length > 0 ? roots[0] : null;
            };
            const summarizeAction = (element, source, root, evidence) => {
                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                return {
                    source,
                    text: getElementText(element).slice(0, 260),
                    rootText: root ? root.text.slice(0, 520) : '',
                    tagName: element.tagName.toLowerCase(),
                    role: element.getAttribute('role') || '',
                    id: element.id || '',
                    className: getClassText(element).slice(0, 260),
                    configuredXPathEvidence: evidence ? evidence.configuredXPathEvidence : 0,
                    closeActionEvidence: evidence ? evidence.closeActionEvidence : 0,
                    primaryRewardedCloseEvidence: evidence ? evidence.primaryRewardedCloseEvidence : 0,
                    negativeActionEvidence: evidence ? evidence.negativeActionEvidence : 0,
                    pointerReceivable: isPointerReceivable(element),
                    cursor: style.cursor,
                    area: rect.width * rect.height,
                    top: rect.top,
                    left: rect.left,
                };
            };

            const configuredCloseButton = resolveXPathElement(adOverlayCloseButtonXPathValue);
            const adInterruptionRoot = findAdInterruptionRoot() || buildRootSummary();
            const baseCandidates = [];
            if (configuredCloseButton instanceof HTMLElement && isVisible(configuredCloseButton)) {
                baseCandidates.push({
                    element: configuredCloseButton,
                    marker: `${markerPrefix}-configured-overlay-close-xpath`,
                    configuredXPathEvidence: 1,
                    closeActionEvidence: 1,
                    primaryRewardedCloseEvidence: configuredCloseButton.id === 'close_button'
                        || configuredCloseButton.id === 'close_button_icon'
                        || configuredCloseButton.closest('#reward_close_button_widget')
                        ? 1
                        : 0,
                    negativeActionEvidence: 0,
                    pointerReceivable: isPointerReceivable(configuredCloseButton),
                    source: 'configuredAdOverlayCloseButtonXPath',
                });
            }

            const semanticSearchRoot = adInterruptionRoot && adInterruptionRoot.element ? adInterruptionRoot.element : document;
            baseCandidates.push(
                ...Array.from(semanticSearchRoot.querySelectorAll(actionableSelector))
                    .filter((element) => element instanceof HTMLElement)
                    .filter(isVisible)
                    .map((element, index) => {
                        const text = getElementText(element);
                        return {
                            element,
                            marker: `${markerPrefix}-semantic-${index}`,
                            configuredXPathEvidence: 0,
                            closeActionEvidence: getEvidenceCount(closeActionPatterns, text),
                            primaryRewardedCloseEvidence: element.id === 'close_button'
                                || element.id === 'close_button_icon'
                                || Boolean(element.closest('#reward_close_button_widget'))
                                ? 1
                                : 0,
                            negativeActionEvidence: getEvidenceCount(negativeActionPatterns, text),
                            pointerReceivable: isPointerReceivable(element),
                            source: 'semanticAdInterruptionCloseAction',
                        };
                    })
            );

            const candidates = baseCandidates
                .filter((candidate, index, allCandidates) => index === allCandidates.findIndex((other) => other.element === candidate.element))
                .map((candidate) => {
                    const rect = candidate.element.getBoundingClientRect();
                    return {
                        ...candidate,
                        area: rect.width * rect.height,
                        top: rect.top,
                        left: rect.left,
                    };
                })
                .filter((candidate) => candidate.configuredXPathEvidence > 0
                    || candidate.primaryRewardedCloseEvidence > 0
                    || candidate.closeActionEvidence > 0)
                .filter((candidate) => candidate.negativeActionEvidence === 0)
                .sort((left, right) => {
                    const comparisons = [
                        right.configuredXPathEvidence - left.configuredXPathEvidence,
                        right.primaryRewardedCloseEvidence - left.primaryRewardedCloseEvidence,
                        right.closeActionEvidence - left.closeActionEvidence,
                        Number(right.pointerReceivable) - Number(left.pointerReceivable),
                        right.area - left.area,
                        right.top - left.top,
                        left.left - right.left,
                    ];
                    return comparisons.find((comparison) => comparison !== 0) || 0;
                });

            if (candidates.length === 0) {
                return {
                    ok: false,
                    reason: 'No visible configured or semantic ad close action was resolved.',
                    adOverlayCloseButtonXPath: adOverlayCloseButtonXPathValue,
                    configuredCloseButtonVisible: configuredCloseButton instanceof HTMLElement ? isVisible(configuredCloseButton) : false,
                    root: {
                        text: adInterruptionRoot ? adInterruptionRoot.text.slice(0, 520) : '',
                        adInterruptionEvidence: adInterruptionRoot ? adInterruptionRoot.adInterruptionEvidence : 0,
                        closeActionEvidence: adInterruptionRoot ? adInterruptionRoot.closeActionEvidence : 0,
                    },
                    visibleTextSample: document.body ? document.body.innerText.slice(0, 1600) : '',
                };
            }

            const selectedCandidate = candidates[0];
            selectedCandidate.element.setAttribute('data-auto-wikigacha-ad-close', selectedCandidate.marker);
            return {
                ok: true,
                selector: `[data-auto-wikigacha-ad-close="${selectedCandidate.marker}"]`,
                selected: summarizeAction(selectedCandidate.element, selectedCandidate.source, adInterruptionRoot, selectedCandidate),
                candidates: candidates.map((candidate) => summarizeAction(candidate.element, candidate.source, adInterruptionRoot, candidate)),
            };
        }
        """,
        adOverlayCloseButtonXPathValue,
    )



def resolveAdCloseConfirmationTargetInFrame(frame: Any, frameIndex: int) -> dict[str, Any]:
    try:
        frameResolution = frame.evaluate(
            r"""
            ({ frameIndex, frameName, frameUrl }) => {
                const markerPrefix = `auto-wikigacha-frame-ad-close-confirmation-${Date.now()}-${Math.random().toString(36).slice(2)}`;
                const dialogSelector = [
                    '#close_confirmation_dialog',
                    '#dialog_wrapper',
                    '[id*="confirmation" i]',
                    '[role="dialog"]',
                    '[aria-modal="true"]'
                ].join(',');
                const actionSelector = [
                    '#resume_video_button',
                    '#close_video_button',
                    '[id*="resume_video" i]',
                    '[id*="close_video" i]',
                    '[role="button"]',
                    'button',
                    '[tabindex]'
                ].join(',');
                const confirmationPatterns = [
                    /要關閉影片嗎|要关闭影片吗/iu,
                    /關閉影片嗎|关闭影片吗/iu,
                    /close\s*(?:the\s*)?video\?/iu,
                    /close\s*confirmation|confirmation\s*dialog/iu,
                ];
                const rewardLossPatterns = [
                    /無法獲得獎勵|无法获得奖励/iu,
                    /無法.*獎勵|无法.*奖励/iu,
                    /失去.*獎勵|失去.*奖励/iu,
                    /lose.*reward|without.*reward|cannot.*reward|won'?t.*reward/iu,
                ];
                const resumeActionPatterns = [
                    /繼續觀看|继续观看/iu,
                    /繼續.*看|继续.*看/iu,
                    /resume[_\s-]*video/iu,
                    /continue\s*(?:watching|video)/iu,
                    /^resume_video_button$/iu,
                ];
                const closeActionPatterns = [
                    /關閉影片|关闭影片/iu,
                    /close[_\s-]*video/iu,
                    /^close_video_button$/iu,
                ];
                const negativeActionPatterns = [
                    /瞭解詳情|了解详情|learn\s*more|details/iu,
                    /廣告|广告|ad\s*choices/iu,
                ];
                const normalizeWhitespace = (value) => String(value || '').replace(/\s+/g, ' ').trim();
                const getClassText = (element) => typeof element.className === 'string' ? element.className : '';
                const getElementText = (element) => {
                    if (!element) {
                        return '';
                    }
                    return normalizeWhitespace([
                        element.innerText,
                        element.textContent,
                        element.getAttribute ? element.getAttribute('aria-label') : '',
                        element.getAttribute ? element.getAttribute('title') : '',
                        element.getAttribute ? element.getAttribute('value') : '',
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
                    const rectangles = Array.from(element.getClientRects()).filter((rect) => rect.width > 0 && rect.height > 0);
                    const targetRectangles = rectangles.length > 0 ? rectangles : [element.getBoundingClientRect()];
                    return targetRectangles.some((rect) => {
                        const centerX = rect.left + rect.width / 2;
                        const centerY = rect.top + rect.height / 2;
                        const hitElement = document.elementFromPoint(centerX, centerY);
                        return Boolean(hitElement && (element === hitElement || element.contains(hitElement) || hitElement.contains(element)));
                    });
                };
                const getEvidenceCount = (patterns, text) => patterns
                    .map((pattern) => pattern.test(text))
                    .filter(Boolean)
                    .length;
                const visibleDialogs = Array.from(document.querySelectorAll(dialogSelector))
                    .filter((element) => element instanceof HTMLElement)
                    .filter(isVisible)
                    .map((element) => {
                        const text = getElementText(element);
                        const combinedText = `${location.href} ${document.title || ''} ${text}`;
                        const rect = element.getBoundingClientRect();
                        return {
                            element,
                            text,
                            confirmationEvidence: getEvidenceCount(confirmationPatterns, combinedText),
                            rewardLossEvidence: getEvidenceCount(rewardLossPatterns, combinedText),
                            area: rect.width * rect.height,
                            top: rect.top,
                            left: rect.left,
                        };
                    })
                    .filter((dialog) => dialog.confirmationEvidence > 0 || dialog.rewardLossEvidence > 0)
                    .sort((left, right) => {
                        const comparisons = [
                            right.rewardLossEvidence - left.rewardLossEvidence,
                            right.confirmationEvidence - left.confirmationEvidence,
                            right.area - left.area,
                            left.top - right.top,
                            left.left - right.left,
                        ];
                        return comparisons.find((comparison) => comparison !== 0) || 0;
                    });
                const bodyText = getElementText(document.body);
                const bodyEvidence = {
                    confirmationEvidence: getEvidenceCount(confirmationPatterns, `${location.href} ${document.title || ''} ${bodyText}`),
                    rewardLossEvidence: getEvidenceCount(rewardLossPatterns, `${location.href} ${document.title || ''} ${bodyText}`),
                };
                const selectedDialog = visibleDialogs.length > 0
                    ? visibleDialogs[0]
                    : bodyEvidence.confirmationEvidence > 0 || bodyEvidence.rewardLossEvidence > 0
                        ? {
                            element: document.body,
                            text: bodyText,
                            confirmationEvidence: bodyEvidence.confirmationEvidence,
                            rewardLossEvidence: bodyEvidence.rewardLossEvidence,
                            area: 0,
                            top: 0,
                            left: 0,
                        }
                        : null;
                if (!selectedDialog) {
                    return {
                        ok: false,
                        reason: 'No rewarded-ad close-confirmation dialog was found in this frame.',
                        targetScope: 'frame',
                        frameIndex,
                        frameName,
                        frameUrl,
                        visibleTextSample: bodyText.slice(0, 1000),
                    };
                }
                const actionRoot = selectedDialog.element === document.body ? document : selectedDialog.element;
                const actions = Array.from(actionRoot.querySelectorAll(actionSelector))
                    .filter((element) => element instanceof HTMLElement)
                    .filter(isVisible)
                    .map((element, index) => {
                        const text = getElementText(element);
                        const resumeEvidence = getEvidenceCount(resumeActionPatterns, text);
                        const closeEvidence = getEvidenceCount(closeActionPatterns, text);
                        const negativeEvidence = getEvidenceCount(negativeActionPatterns, text);
                        const rect = element.getBoundingClientRect();
                        return {
                            element,
                            marker: `${markerPrefix}-${index}`,
                            text,
                            resumeEvidence,
                            closeEvidence,
                            negativeEvidence,
                            pointerReceivable: isPointerReceivable(element),
                            area: rect.width * rect.height,
                            top: rect.top,
                            left: rect.left,
                        };
                    })
                    .filter((candidate, index, allCandidates) => index === allCandidates.findIndex((other) => other.element === candidate.element))
                    .filter((candidate) => candidate.negativeEvidence === 0)
                    .filter((candidate) => {
                        if (selectedDialog.rewardLossEvidence > 0) {
                            return candidate.closeEvidence > 0 || candidate.resumeEvidence > 0;
                        }
                        return candidate.resumeEvidence > 0 || candidate.closeEvidence > 0;
                    })
                    .sort((left, right) => {
                        const comparisons = selectedDialog.rewardLossEvidence > 0
                            ? [
                                right.closeEvidence - left.closeEvidence,
                                right.resumeEvidence - left.resumeEvidence,
                                Number(right.pointerReceivable) - Number(left.pointerReceivable),
                                right.area - left.area,
                                left.left - right.left,
                            ]
                            : [
                                right.closeEvidence - left.closeEvidence,
                                right.resumeEvidence - left.resumeEvidence,
                                Number(right.pointerReceivable) - Number(left.pointerReceivable),
                                right.area - left.area,
                                left.left - right.left,
                            ];
                        return comparisons.find((comparison) => comparison !== 0) || 0;
                    });
                const summarizeAction = (candidate) => {
                    const rect = candidate.element.getBoundingClientRect();
                    const style = window.getComputedStyle(candidate.element);
                    return {
                        targetScope: 'frame',
                        frameIndex,
                        frameName,
                        frameUrl,
                        source: candidate.closeEvidence > 0
                            ? 'rewardedAdCloseConfirmationCloseButton'
                            : 'rewardedAdCloseConfirmationResumeButton',
                        selector: candidate.selector,
                        actionIntent: candidate.closeEvidence > 0
                            ? selectedDialog.rewardLossEvidence > 0
                                ? 'closeVideoAfterRewardLossConfirmation'
                                : 'closeVideoAfterConfirmation'
                            : 'resumeVideo',
                        text: candidate.text.slice(0, 260),
                        dialogText: selectedDialog.text.slice(0, 520),
                        tagName: candidate.element.tagName.toLowerCase(),
                        role: candidate.element.getAttribute('role') || '',
                        id: candidate.element.id || '',
                        className: getClassText(candidate.element).slice(0, 260),
                        confirmationEvidence: selectedDialog.confirmationEvidence,
                        rewardLossEvidence: selectedDialog.rewardLossEvidence,
                        resumeEvidence: candidate.resumeEvidence,
                        closeEvidence: candidate.closeEvidence,
                        negativeEvidence: candidate.negativeEvidence,
                        pointerReceivable: candidate.pointerReceivable,
                        cursor: style.cursor,
                        area: rect.width * rect.height,
                        top: rect.top,
                        left: rect.left,
                    };
                };
                if (actions.length === 0) {
                    return {
                        ok: false,
                        reason: selectedDialog.rewardLossEvidence > 0
                            ? 'Reward-loss close-confirmation dialog was found, but no semantic close-or-resume action was resolved.'
                            : 'Close-confirmation dialog was found, but no semantic confirmation action was resolved.',
                        targetScope: 'frame',
                        frameIndex,
                        frameName,
                        frameUrl,
                        dialog: {
                            text: selectedDialog.text.slice(0, 520),
                            confirmationEvidence: selectedDialog.confirmationEvidence,
                            rewardLossEvidence: selectedDialog.rewardLossEvidence,
                        },
                        visibleTextSample: bodyText.slice(0, 1000),
                    };
                }
                const selectedAction = actions[0];
                selectedAction.element.setAttribute('data-auto-wikigacha-frame-close-confirmation', selectedAction.marker);
                selectedAction.selector = `[data-auto-wikigacha-frame-close-confirmation="${selectedAction.marker}"]`;
                return {
                    ok: true,
                    targetScope: 'frame',
                    frameIndex,
                    frameName,
                    frameUrl,
                    selector: selectedAction.selector,
                    selected: summarizeAction(selectedAction),
                    candidates: actions.map((candidate) => {
                        candidate.selector = candidate.selector || '';
                        return summarizeAction(candidate);
                    }),
                };
            }
            """,
            {
                "frameIndex": frameIndex,
                "frameName": frame.name,
                "frameUrl": frame.url,
            },
        )
        return frameResolution
    except PlaywrightError as error:
        return {
            "ok": False,
            "targetScope": "frame",
            "frameIndex": frameIndex,
            "frameName": getattr(frame, "name", ""),
            "frameUrl": getattr(frame, "url", ""),
            "reason": "Frame close-confirmation inspection failed.",
            "errorType": type(error).__name__,
            "errorMessage": str(error),
        }


def resolveAdCloseConfirmationTarget(page: Page) -> dict[str, Any]:
    frameDiagnostics: list[dict[str, Any]] = []
    for frameIndex, frame in enumerate(page.frames):
        if frame == page.main_frame:
            continue
        frameResolution = resolveAdCloseConfirmationTargetInFrame(frame, frameIndex)
        if frameResolution.get("ok"):
            return frameResolution
        frameDiagnostics.append(frameResolution)
    return {
        "ok": False,
        "reason": "No rewarded-ad close-confirmation target was found in any child frame.",
        "targetScope": "frame",
        "frameDiagnostics": frameDiagnostics,
    }


def resolveAdCloseSelectorInFrame(frame: Any, frameIndex: int) -> dict[str, Any]:
    try:
        frameResolution = frame.evaluate(
            r"""
            ({ frameIndex, frameName, frameUrl }) => {
                const markerPrefix = `auto-wikigacha-frame-ad-close-${Date.now()}-${Math.random().toString(36).slice(2)}`;
                const closeCandidateSelector = [
                    '#reward_close_button_widget #close_button',
                    '#reward_close_button_widget [role="button"]',
                    '#close_button[aria-label]',
                    '#close_button[role="button"]',
                    '#close_button[tabindex]',
                    '#close_button',
                    '#close_button_icon',
                    '[aria-label="關閉影片"]',
                    '[aria-label="关闭影片"]',
                    '[aria-label*="關閉影片"]',
                    '[aria-label*="关闭影片"]',
                    '[aria-label*="關閉廣告"]',
                    '[aria-label*="关闭广告"]',
                    '[aria-label*="close video" i]',
                    '[aria-label*="close ad" i]',
                    '[aria-label*="close" i]',
                    '[id*="close" i][role="button"]',
                    '[id*="close" i][tabindex]',
                    '[class*="close" i][role="button"]',
                    '[class*="close" i][tabindex]',
                    '[role="button"]',
                    '[tabindex]'
                ].join(',');
                const closeEvidencePatterns = [
                    /關閉影片|关闭影片/iu,
                    /close\s*video/iu,
                    /關閉廣告|关闭广告/iu,
                    /close\s*(?:ad|advertisement)/iu,
                    /^close_button(?:_icon)?$/iu,
                    /reward_close_button_widget/iu,
                    /rewarded[_-]?ad[_-]?close/iu,
                    /^\s*[×✕✖x]\s*$/iu,
                ];
                const frameEvidencePatterns = [
                    /googlesyndication|safeframe|goog_rewarded|rewarded|google/iu,
                    /Google\s*AI\s*Plus|one\.google\.com/iu,
                    /reward_close_button_widget|close_button_icon|close_button/iu,
                    /ad|ads|advertisement/iu,
                ];
                const negativePatterns = [
                    /瞭解詳情|了解详情|learn\s*more|details/iu,
                    /YAMAHA|JOG|葉黃素|新聞|news/iu,
                ];
                const rewardPendingPatterns = [
                    /(?:\d+|[０-９]+)\s*秒(?:後|后).*?(?:獎勵|奖励)/iu,
                    /秒(?:後|后)可(?:獲|获)(?:得)?(?:獎勵|奖励)/iu,
                    /reward.*(?:in|after)\s*(?:\d+|[０-９]+)\s*(?:s|sec|second)/iu,
                    /(?:wait|available).*?(?:\d+|[０-９]+).*?(?:reward|second|sec)/iu,
                ];
                const normalizeWhitespace = (value) => String(value || '').replace(/\s+/g, ' ').trim();
                const getClassText = (element) => typeof element.className === 'string' ? element.className : '';
                const getElementText = (element) => {
                    if (!element) {
                        return '';
                    }
                    return normalizeWhitespace([
                        element.innerText,
                        element.textContent,
                        element.getAttribute ? element.getAttribute('aria-label') : '',
                        element.getAttribute ? element.getAttribute('title') : '',
                        element.getAttribute ? element.getAttribute('value') : '',
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
                    const rectangles = Array.from(element.getClientRects()).filter((rect) => rect.width > 0 && rect.height > 0);
                    const targetRectangles = rectangles.length > 0 ? rectangles : [element.getBoundingClientRect()];
                    return targetRectangles.some((rect) => {
                        const centerX = rect.left + rect.width / 2;
                        const centerY = rect.top + rect.height / 2;
                        const hitElement = document.elementFromPoint(centerX, centerY);
                        return Boolean(hitElement && (element === hitElement || element.contains(hitElement) || hitElement.contains(element)));
                    });
                };
                const getEvidenceCount = (patterns, text) => patterns
                    .map((pattern) => pattern.test(text))
                    .filter(Boolean)
                    .length;
                const summarizeCandidate = (candidate) => {
                    const rect = candidate.element.getBoundingClientRect();
                    const style = window.getComputedStyle(candidate.element);
                    return {
                        targetScope: 'frame',
                        frameIndex,
                        frameName,
                        frameUrl,
                        source: candidate.source,
                        selector: candidate.selector,
                        text: candidate.text.slice(0, 260),
                        tagName: candidate.element.tagName.toLowerCase(),
                        role: candidate.element.getAttribute('role') || '',
                        id: candidate.element.id || '',
                        className: getClassText(candidate.element).slice(0, 260),
                        closeEvidence: candidate.closeEvidence,
                        frameEvidence: candidate.frameEvidence,
                        rewardPendingEvidence: candidate.rewardPendingEvidence,
                        isPrimaryRewardedCloseButton: candidate.isPrimaryRewardedCloseButton,
                        negativeEvidence: candidate.negativeEvidence,
                        pointerReceivable: isPointerReceivable(candidate.element),
                        cursor: style.cursor,
                        area: rect.width * rect.height,
                        top: rect.top,
                        left: rect.left,
                    };
                };
                const frameBodyText = getElementText(document.body);
                const rewardPendingEvidence = getEvidenceCount(
                    rewardPendingPatterns,
                    `${location.href} ${document.title || ''} ${frameBodyText}`,
                );
                const candidates = Array.from(document.querySelectorAll(closeCandidateSelector))
                    .filter((element) => element instanceof HTMLElement)
                    .filter(isVisible)
                    .map((element, index) => {
                        const text = getElementText(element);
                        const frameContextText = `${location.href} ${document.title || ''} ${text}`;
                        const idEvidence = element.id === 'close_button' ? 2 : element.id === 'close_button_icon' ? 1 : 0;
                        const widgetEvidence = element.closest('#reward_close_button_widget') ? 2 : 0;
                        const ariaEvidence = /關閉影片|关闭影片|close\s*video/iu.test(element.getAttribute('aria-label') || '') ? 3 : 0;
                        const closeEvidence = idEvidence + widgetEvidence + ariaEvidence + getEvidenceCount(closeEvidencePatterns, text);
                        const frameEvidence = getEvidenceCount(frameEvidencePatterns, frameContextText);
                        const negativeEvidence = getEvidenceCount(negativePatterns, text);
                        const isPrimaryRewardedCloseButton = idEvidence > 0 || widgetEvidence > 0 || ariaEvidence > 0;
                        return {
                            element,
                            marker: `${markerPrefix}-${index}`,
                            text,
                            selector: '',
                            closeEvidence,
                            frameEvidence,
                            rewardPendingEvidence,
                            isPrimaryRewardedCloseButton,
                            negativeEvidence,
                            source: isPrimaryRewardedCloseButton
                                ? 'googleRewardedFrameCloseButton'
                                : 'semanticFrameCloseButton',
                        };
                    })
                    .filter((candidate, index, allCandidates) => index === allCandidates.findIndex((other) => other.element === candidate.element))
                    .filter((candidate) => candidate.negativeEvidence === 0)
                    .filter((candidate) => candidate.closeEvidence > 0 || candidate.frameEvidence > 0)
                    .sort((left, right) => {
                        const comparisons = [
                            right.closeEvidence - left.closeEvidence,
                            right.frameEvidence - left.frameEvidence,
                            Number(isPointerReceivable(right.element)) - Number(isPointerReceivable(left.element)),
                            right.element.getBoundingClientRect().top - left.element.getBoundingClientRect().top,
                            right.element.getBoundingClientRect().left - left.element.getBoundingClientRect().left,
                        ];
                        return comparisons.find((comparison) => comparison !== 0) || 0;
                    });
                if (candidates.length === 0) {
                    return {
                        ok: false,
                        reason: 'No visible frame-level rewarded-ad close button was found.',
                        targetScope: 'frame',
                        frameIndex,
                        frameName,
                        frameUrl,
                        rewardPendingEvidence,
                        visibleTextSample: document.body ? document.body.innerText.slice(0, 1000) : '',
                    };
                }
                const selectedCandidate = candidates[0];
                selectedCandidate.element.setAttribute('data-auto-wikigacha-frame-ad-close', selectedCandidate.marker);
                selectedCandidate.selector = `[data-auto-wikigacha-frame-ad-close="${selectedCandidate.marker}"]`;
                return {
                    ok: true,
                    targetScope: 'frame',
                    frameIndex,
                    frameName,
                    frameUrl,
                    selector: selectedCandidate.selector,
                    selected: summarizeCandidate(selectedCandidate),
                    candidates: candidates.map((candidate) => {
                        candidate.selector = candidate.selector || '';
                        return summarizeCandidate(candidate);
                    }),
                };
            }
            """,
            {
                "frameIndex": frameIndex,
                "frameName": frame.name,
                "frameUrl": frame.url,
            },
        )
        return frameResolution
    except PlaywrightError as error:
        return {
            "ok": False,
            "targetScope": "frame",
            "frameIndex": frameIndex,
            "frameName": getattr(frame, "name", ""),
            "frameUrl": getattr(frame, "url", ""),
            "reason": "Frame close-button inspection failed.",
            "errorType": type(error).__name__,
            "errorMessage": str(error),
        }


def resolveAdInterruptionCloseTarget(page: Page, adOverlayCloseButtonXPathValue: str) -> dict[str, Any]:
    closeConfirmationDiagnostics: list[dict[str, Any]] = []
    for frameIndex, frame in enumerate(page.frames):
        if frame == page.main_frame:
            continue
        closeConfirmationResolution = resolveAdCloseConfirmationTargetInFrame(frame, frameIndex)
        if closeConfirmationResolution.get("ok"):
            return closeConfirmationResolution
        closeConfirmationDiagnostics.append(closeConfirmationResolution)

    pageResolution = resolveAdInterruptionCloseSelector(page, adOverlayCloseButtonXPathValue)
    if pageResolution.get("ok"):
        pageResolution["targetScope"] = "page"
        pageResolution["frameCloseConfirmationDiagnostics"] = closeConfirmationDiagnostics
        return pageResolution

    frameDiagnostics: list[dict[str, Any]] = []
    for frameIndex, frame in enumerate(page.frames):
        if frame == page.main_frame:
            continue
        frameResolution = resolveAdCloseSelectorInFrame(frame, frameIndex)
        if frameResolution.get("ok"):
            frameResolution["frameCloseConfirmationDiagnostics"] = closeConfirmationDiagnostics
            return frameResolution
        frameDiagnostics.append(frameResolution)

    pageResolution["targetScope"] = "page"
    pageResolution["frameCloseConfirmationDiagnostics"] = closeConfirmationDiagnostics
    pageResolution["frameDiagnostics"] = frameDiagnostics
    return pageResolution


def resolveFrameForResolution(page: Page, resolution: dict[str, Any]) -> Any | None:
    frameIndex = resolution.get("frameIndex")
    frameUrl = resolution.get("frameUrl")
    frameName = resolution.get("frameName")
    frames = page.frames
    if isinstance(frameIndex, int) and 0 <= frameIndex < len(frames):
        indexedFrame = frames[frameIndex]
        if (not frameUrl or indexedFrame.url == frameUrl) and (frameName is None or indexedFrame.name == frameName):
            return indexedFrame
    for frame in frames:
        if frameUrl and frame.url == frameUrl and (frameName is None or frame.name == frameName):
            return frame
    for frame in frames:
        if frameUrl and frameUrl in frame.url:
            return frame
    return None


def clickSelectorUsingFreshFrameElement(page: Page, resolution: dict[str, Any]) -> dict[str, Any]:
    frame = resolveFrameForResolution(page, resolution)
    selector = str(resolution.get("selector") or "")
    if frame is None:
        return {
            "ok": False,
            "reason": "frameCouldNotBeResolvedForAdCloseTarget",
            "selectorRefreshRecommended": True,
            "targetScope": "frame",
            "frameIndex": resolution.get("frameIndex"),
            "frameName": resolution.get("frameName"),
            "frameUrl": resolution.get("frameUrl"),
            "selector": selector,
        }
    try:
        return frame.evaluate(
            r"""
            (selector) => new Promise((resolve) => {
                const normalizeWhitespace = (value) => String(value || '').replace(/\s+/g, ' ').trim();
                const summarizeElement = (element) => {
                    if (!(element instanceof HTMLElement)) {
                        return null;
                    }
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return {
                        tagName: element.tagName.toLowerCase(),
                        id: element.id || '',
                        className: typeof element.className === 'string' ? element.className.slice(0, 260) : '',
                        role: element.getAttribute('role') || '',
                        ariaLabel: element.getAttribute('aria-label') || '',
                        text: normalizeWhitespace([
                            element.innerText,
                            element.textContent,
                            element.getAttribute('aria-label'),
                            element.getAttribute('title'),
                            element.getAttribute('value'),
                        ].filter(Boolean).join(' ')).slice(0, 260),
                        connected: element.isConnected,
                        rect: {
                            left: rect.left,
                            top: rect.top,
                            right: rect.right,
                            bottom: rect.bottom,
                            width: rect.width,
                            height: rect.height,
                        },
                        display: style.display,
                        visibility: style.visibility,
                        opacity: style.opacity,
                        pointerEvents: style.pointerEvents,
                    };
                };
                const isUsableElement = (element) => {
                    if (!(element instanceof HTMLElement)) {
                        return { ok: false, reason: 'selectorDidNotResolveToHTMLElement' };
                    }
                    if (!element.isConnected || !document.documentElement.contains(element)) {
                        return { ok: false, reason: 'elementIsDetachedFromDom' };
                    }
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    if (style.visibility === 'hidden' || style.display === 'none' || rect.width <= 0 || rect.height <= 0) {
                        return { ok: false, reason: 'elementIsNotRendered' };
                    }
                    if (element.hasAttribute('disabled') || element.getAttribute('aria-disabled') === 'true') {
                        return { ok: false, reason: 'elementIsDisabled' };
                    }
                    return { ok: true, reason: 'elementIsFreshAndUsable' };
                };
                const getPointerReceivable = (element) => {
                    const rectangles = Array.from(element.getClientRects()).filter((rect) => rect.width > 0 && rect.height > 0);
                    const targetRectangles = rectangles.length > 0 ? rectangles : [element.getBoundingClientRect()];
                    return targetRectangles.some((rect) => {
                        const centerX = rect.left + rect.width / 2;
                        const centerY = rect.top + rect.height / 2;
                        const hitElement = document.elementFromPoint(centerX, centerY);
                        return Boolean(hitElement && (element === hitElement || element.contains(hitElement) || hitElement.contains(element)));
                    });
                };
                const resolveCurrentElement = () => document.querySelector(selector);
                const initialElement = resolveCurrentElement();
                const initialValidation = isUsableElement(initialElement);
                if (!initialValidation.ok) {
                    resolve({
                        ok: false,
                        reason: initialValidation.reason,
                        selector,
                        targetScope: 'frame',
                        selectorRefreshRecommended: true,
                        elementSummary: summarizeElement(initialElement),
                    });
                    return;
                }
                window.requestAnimationFrame(() => {
                    const freshElement = resolveCurrentElement();
                    const freshValidation = isUsableElement(freshElement);
                    if (!freshValidation.ok) {
                        resolve({
                            ok: false,
                            reason: freshValidation.reason,
                            selector,
                            targetScope: 'frame',
                            selectorRefreshRecommended: true,
                            elementSummary: summarizeElement(freshElement),
                        });
                        return;
                    }
                    const pointerReceivable = getPointerReceivable(freshElement);
                    try {
                        freshElement.click();
                        resolve({
                            ok: true,
                            reason: 'clickedFreshFrameDomElement',
                            selector,
                            targetScope: 'frame',
                            clickMethod: 'HTMLElement.click',
                            pointerReceivable,
                            elementSummary: summarizeElement(freshElement),
                        });
                    } catch (error) {
                        resolve({
                            ok: false,
                            reason: 'frameDomClickRaisedException',
                            selector,
                            targetScope: 'frame',
                            selectorRefreshRecommended: true,
                            errorName: error && error.name ? error.name : '',
                            errorMessage: error && error.message ? error.message : String(error),
                            pointerReceivable,
                            elementSummary: summarizeElement(freshElement),
                        });
                    }
                });
            })
            """,
            selector,
        )
    except PlaywrightError as error:
        return {
            "ok": False,
            "reason": "frameDomClickEvaluationFailed",
            "selectorRefreshRecommended": True,
            "targetScope": "frame",
            "selector": selector,
            "frameIndex": resolution.get("frameIndex"),
            "frameName": resolution.get("frameName"),
            "frameUrl": resolution.get("frameUrl"),
            "errorType": type(error).__name__,
            "errorMessage": str(error),
        }


def clickResolvedAdCloseTargetAndWait(
    page: Page,
    resolution: dict[str, Any],
    refreshResolution: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    previousFingerprint = getPageFingerprint(page)
    previousRenderedStateFingerprint = getRenderedStateFingerprint(page)
    currentResolution = resolution
    clickAttempts: list[dict[str, Any]] = []
    refreshedResolutions: list[dict[str, Any]] = []
    seenRefreshedResolutionFingerprints: set[str] = set()

    while True:
        if currentResolution.get("targetScope") == "frame":
            clickAttempt = clickSelectorUsingFreshFrameElement(page, currentResolution)
        else:
            clickAttempt = clickSelectorUsingFreshDomElement(page, currentResolution["selector"])
        clickAttempts.append(clickAttempt)
        if clickAttempt.get("ok"):
            break
        if not clickAttempt.get("selectorRefreshRecommended"):
            raise WikiGachaAutomationError(
                "Resolved ad-close selector could not be clicked and did not expose a refresh path."
            )
        refreshedResolution = refreshResolution()
        refreshedResolutions.append(refreshedResolution)
        if not refreshedResolution.get("ok"):
            raise WikiGachaAutomationError(
                refreshedResolution.get(
                    "reason",
                    "Resolved ad-close selector became stale and no fresh replacement target could be resolved.",
                )
            )
        refreshedResolutionFingerprint = buildResolutionFingerprint(refreshedResolution)
        if refreshedResolutionFingerprint in seenRefreshedResolutionFingerprints:
            raise WikiGachaAutomationError(
                "Resolved ad-close selector repeatedly became stale after semantic refresh."
            )
        seenRefreshedResolutionFingerprints.add(refreshedResolutionFingerprint)
        currentResolution = refreshedResolution

    renderObservation = waitForRenderCycle(page)
    currentFingerprint = getPageFingerprint(page)
    currentRenderedStateFingerprint = getRenderedStateFingerprint(page)
    pageFingerprintChanged = previousFingerprint != currentFingerprint
    renderedStateChanged = previousRenderedStateFingerprint != currentRenderedStateFingerprint
    stateChanged = pageFingerprintChanged or renderedStateChanged
    return {
        "previousFingerprintHash": buildShortHash(previousFingerprint),
        "currentFingerprintHash": buildShortHash(currentFingerprint),
        "previousRenderedStateFingerprintHash": buildShortHash(previousRenderedStateFingerprint),
        "currentRenderedStateFingerprintHash": buildShortHash(currentRenderedStateFingerprint),
        "pageFingerprintChanged": pageFingerprintChanged,
        "renderedStateChanged": renderedStateChanged,
        "stateChanged": stateChanged,
        "fingerprintChanged": stateChanged,
        "clickCompleted": True,
        "clickAborted": False,
        "initialResolution": resolution,
        "finalResolution": currentResolution,
        "clickAttempts": clickAttempts,
        "refreshedResolutions": refreshedResolutions,
        "renderObservation": renderObservation,
    }


def waitForAdOutcomeMutationOrPaint(page: Page) -> None:
    page.evaluate(
        r"""
        () => new Promise((resolve) => {
            let resolved = false;
            const finish = () => {
                if (resolved) {
                    return;
                }
                resolved = true;
                if (observer) {
                    observer.disconnect();
                }
                requestAnimationFrame(() => requestAnimationFrame(resolve));
            };
            const observer = document.documentElement
                ? new MutationObserver(finish)
                : null;
            if (observer && document.documentElement) {
                observer.observe(document.documentElement, {
                    attributes: true,
                    childList: true,
                    characterData: true,
                    subtree: true,
                });
            }
            requestAnimationFrame(finish);
        })
        """
    )


def shouldObserveAfterGoogleRewardedAdClose(adInterruptionResolution: dict[str, Any]) -> bool:
    selectedResolution = adInterruptionResolution.get("selected", {})
    if not isinstance(selectedResolution, dict):
        return False

    selectedSource = str(selectedResolution.get("source") or "")
    selectedId = str(selectedResolution.get("id") or "")
    selectedClassName = str(selectedResolution.get("className") or "")
    selectedText = str(selectedResolution.get("text") or "")
    selectedSelector = str(adInterruptionResolution.get("selector") or "")
    combinedTargetText = " ".join(
        [
            selectedSource,
            selectedId,
            selectedClassName,
            selectedText,
            selectedSelector,
            str(selectedResolution.get("ariaLabel") or ""),
        ]
    ).lower()

    primaryEvidence = selectedResolution.get("primaryRewardedCloseEvidence", 0)
    rewardedFrameEvidence = selectedResolution.get("isPrimaryRewardedCloseButton", False)
    rewardPendingEvidence = selectedResolution.get("rewardPendingEvidence", 0)
    return (
        selectedSource == "googleRewardedFrameCloseButton"
        or bool(rewardedFrameEvidence)
        or (isinstance(primaryEvidence, int) and primaryEvidence > 0)
        or (isinstance(rewardPendingEvidence, int) and rewardPendingEvidence > 0)
        or "reward_close_button_widget" in combinedTargetText
        or "close_button_icon" in combinedTargetText
        or "close_button" in combinedTargetText
        or "關閉影片" in combinedTargetText
        or "关闭影片" in combinedTargetText
        or "close video" in combinedTargetText
    )


def waitForGoogleRewardedAdCloseSettlingWindow(page: Page, settlingSeconds: float) -> dict[str, Any]:
    if settlingSeconds <= 0:
        return {
            "ok": True,
            "skipped": True,
            "reason": "googleRewardedAdCloseSettlingSecondsIsZero",
            "requestedSettlingSeconds": settlingSeconds,
        }

    return page.evaluate(
        r"""
        (settlingSeconds) => new Promise((resolve) => {
            const requestedSettlingSeconds = Number(settlingSeconds);
            const requestedSettlingMilliseconds = Math.max(0, requestedSettlingSeconds * 1000);
            const startedAtPerformanceNow = performance.now();
            const startedAtIso = new Date().toISOString();
            let mutationCount = 0;
            let animationFrameCount = 0;
            let resolved = false;

            const summarizeDocumentState = () => ({
                href: location.href,
                title: document.title,
                readyState: document.readyState,
                visibilityState: document.visibilityState,
                bodyTextSample: document.body ? String(document.body.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 1000) : '',
            });

            const initialDocumentState = summarizeDocumentState();
            const observer = document.documentElement
                ? new MutationObserver((mutations) => {
                    mutationCount += mutations.length;
                })
                : null;

            const finish = (settledBy) => {
                if (resolved) {
                    return;
                }
                resolved = true;
                if (observer) {
                    observer.disconnect();
                }
                window.requestAnimationFrame(() => {
                    window.requestAnimationFrame(() => {
                        const completedAtPerformanceNow = performance.now();
                        resolve({
                            ok: true,
                            skipped: false,
                            requestedSettlingSeconds,
                            observedSettlingSeconds: (completedAtPerformanceNow - startedAtPerformanceNow) / 1000,
                            startedAtIso,
                            completedAtIso: new Date().toISOString(),
                            mutationCount,
                            animationFrameCount,
                            settledBy,
                            initialDocumentState,
                            finalDocumentState: summarizeDocumentState(),
                        });
                    });
                });
            };

            if (observer && document.documentElement) {
                observer.observe(document.documentElement, {
                    attributes: true,
                    childList: true,
                    characterData: true,
                    subtree: true,
                });
            }

            const timeoutIdentifier = window.setTimeout(() => {
                finish('observedSettlingWindowElapsed');
            }, requestedSettlingMilliseconds);

            const observeFrame = () => {
                if (resolved) {
                    window.clearTimeout(timeoutIdentifier);
                    return;
                }
                animationFrameCount += 1;
                window.requestAnimationFrame(observeFrame);
            };
            window.requestAnimationFrame(observeFrame);
        })
        """,
        settlingSeconds,
    )



def resolvePackReadyAfterAdRecoveryOutcome(
    page: Page,
    remainingCountXPath: str,
    insufficientPackHeadingXPathValue: str,
    recoverPackButtonXPathValue: str,
    returnButtonXPath: str,
) -> dict[str, Any]:
    remainingPackResolution = resolveRemainingPackCount(
        page,
        remainingCountXPath,
        insufficientPackHeadingXPathValue,
    )
    remainingPackCount = getRemainingPackCountValue(remainingPackResolution)
    if isinstance(remainingPackCount, int) and remainingPackCount > 0:
        return {
            "ok": True,
            "outcomeType": "remainingPackCountBecamePositive",
            "remainingPackResolution": remainingPackResolution,
        }

    drawTargetResolution = resolveDrawTargetSelector(page, returnButtonXPath)
    if drawTargetResolution.get("ok"):
        return {
            "ok": True,
            "outcomeType": "packTargetBecameAvailable",
            "remainingPackResolution": remainingPackResolution,
            "drawTargetResolution": drawTargetResolution,
        }

    insufficientPackRecoveryResolution = resolveInsufficientPackRecoverySelector(
        page,
        insufficientPackHeadingXPathValue,
        recoverPackButtonXPathValue,
    )
    if insufficientPackRecoveryResolution.get("ok"):
        return {
            "ok": True,
            "outcomeType": "insufficientPackRecoveryStillAvailable",
            "remainingPackResolution": remainingPackResolution,
            "drawTargetResolution": drawTargetResolution,
            "insufficientPackRecoveryResolution": insufficientPackRecoveryResolution,
        }

    return {
        "ok": False,
        "reason": "No post-ad pack-ready state was resolved yet.",
        "remainingPackResolution": remainingPackResolution,
        "drawTargetResolution": drawTargetResolution,
        "insufficientPackRecoveryResolution": insufficientPackRecoveryResolution,
    }


def waitForAdRecoveryOutcomeTarget(
    page: Page,
    adRewardConfirmButtonXPathValue: str,
    adOverlayCloseButtonXPathValue: str,
    remainingCountXPath: str | None = None,
    insufficientPackHeadingXPathValue: str | None = None,
    returnButtonXPath: str | None = None,
    recoverPackButtonXPathValue: str | None = None,
) -> dict[str, Any]:
    while True:
        adCloseResolution = resolveAdInterruptionCloseTarget(page, adOverlayCloseButtonXPathValue)
        if adCloseResolution.get("ok"):
            return {
                "ok": True,
                "outcomeType": "adCloseTargetAvailable",
                "adCloseResolution": adCloseResolution,
            }

        rewardConfirmationResolution = resolveAdRewardConfirmationSelector(page, adRewardConfirmButtonXPathValue)
        if rewardConfirmationResolution.get("ok"):
            return {
                "ok": True,
                "outcomeType": "rewardConfirmationTargetAvailable",
                "rewardConfirmationResolution": rewardConfirmationResolution,
            }

        if remainingCountXPath and insufficientPackHeadingXPathValue and returnButtonXPath and recoverPackButtonXPathValue:
            packReadyOutcome = resolvePackReadyAfterAdRecoveryOutcome(
                page,
                remainingCountXPath,
                insufficientPackHeadingXPathValue,
                recoverPackButtonXPathValue,
                returnButtonXPath,
            )
            if packReadyOutcome.get("ok"):
                return packReadyOutcome

        waitForAdOutcomeMutationOrPaint(page)


def recoverFromAdInterruptionIfPresent(
    page: Page,
    evidencePath: Path,
    arguments: argparse.Namespace,
    drawIndex: int,
) -> bool:
    adInterruptionResolution = resolveAdInterruptionCloseTarget(page, arguments.adOverlayCloseButtonXPath)
    if not adInterruptionResolution.get("ok"):
        return False

    saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_ad_interruption_close_target", adInterruptionResolution)
    if arguments.dryRun:
        print("[DRY-RUN] Ad-interruption close target resolved; close click skipped.")
        return True

    adInterruptionClickPayload = {
        "adInterruptionResolution": adInterruptionResolution,
        **clickResolvedAdCloseTargetAndWait(
            page,
            adInterruptionResolution,
            lambda: resolveAdInterruptionCloseTarget(page, arguments.adOverlayCloseButtonXPath),
        ),
    }
    if shouldObserveAfterGoogleRewardedAdClose(adInterruptionResolution):
        print(
            "[INFO] Google rewarded-ad close button was clicked; observing the requested post-close "
            f"settling window for {arguments.googleRewardedAdCloseSettlingSeconds:g} seconds before continuing."
        )
        postCloseSettlingObservation = waitForGoogleRewardedAdCloseSettlingWindow(
            page,
            arguments.googleRewardedAdCloseSettlingSeconds,
        )
        adInterruptionClickPayload["postGoogleRewardedAdCloseSettlingObservation"] = postCloseSettlingObservation
        saveEvidence(
            page,
            evidencePath,
            f"draw_{drawIndex:03d}_google_rewarded_ad_close_settled",
            {
                "adInterruptionResolution": adInterruptionResolution,
                "postCloseSettlingObservation": postCloseSettlingObservation,
            },
        )

    saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_ad_interruption_closed", adInterruptionClickPayload)
    waitForRenderCycle(page)
    recordAdInterruptionRecoveryAndRestartIfStalled(
        page,
        evidencePath,
        arguments,
        drawIndex,
        adInterruptionResolution,
        adInterruptionClickPayload,
    )
    print("[INFO] Ad-interruption dialog was closed; resuming adaptive recovery.")
    return True


def recoverFromInsufficientPackIfPresent(
    page: Page,
    evidencePath: Path,
    arguments: argparse.Namespace,
    drawIndex: int,
) -> bool:
    recoveryResolution = resolveInsufficientPackRecoverySelector(
        page,
        arguments.insufficientPackHeadingXPath,
        arguments.recoverPackButtonXPath,
    )
    if not recoveryResolution.get("ok"):
        return False

    saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_insufficient_pack_recovery_target", recoveryResolution)
    if arguments.dryRun:
        print("[DRY-RUN] Insufficient-pack recovery target resolved; recovery clicks skipped.")
        return True

    recoveryClickPayload = {
        "recoveryResolution": recoveryResolution,
        **clickResolvedSelectorAndWait(
            page,
            recoveryResolution["selector"],
            lambda: resolveInsufficientPackRecoverySelector(
                page,
                arguments.insufficientPackHeadingXPath,
                arguments.recoverPackButtonXPath,
            ),
        ),
    }
    saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_insufficient_pack_recovery_clicked", recoveryClickPayload)

    adRecoveryOutcome = waitForAdRecoveryOutcomeTarget(
        page,
        arguments.adRewardConfirmButtonXPath,
        arguments.adOverlayCloseButtonXPath,
        arguments.remainingPackCountXPath,
        arguments.insufficientPackHeadingXPath,
        arguments.returnToPackPageXPath,
        arguments.recoverPackButtonXPath,
    )
    saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_ad_recovery_outcome_detected", adRecoveryOutcome)

    if adRecoveryOutcome.get("outcomeType") in {
        "remainingPackCountBecamePositive",
        "packTargetBecameAvailable",
        "insufficientPackRecoveryStillAvailable",
    }:
        resetAdaptiveAdInterruptionRecoveryState("adRecoveryReturnedControlToPackPage")
        print("[INFO] Ad recovery returned control to the pack page; resuming pack opening.")
        return True

    if recoverFromAdInterruptionIfPresent(page, evidencePath, arguments, drawIndex):
        return True

    confirmationResolution = resolveAdRewardConfirmationSelector(page, arguments.adRewardConfirmButtonXPath)
    if not confirmationResolution.get("ok"):
        saveEvidence(
            page,
            evidencePath,
            f"draw_{drawIndex:03d}_ad_reward_confirmation_missing",
            confirmationResolution,
        )
        raise WikiGachaAutomationError(
            confirmationResolution.get("reason", "No ad-reward confirmation button was found after pack recovery was requested.")
        )

    saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_ad_reward_confirmation_target", confirmationResolution)
    confirmationClickPayload = {
        "confirmationResolution": confirmationResolution,
        **clickResolvedSelectorAndWait(
            page,
            confirmationResolution["selector"],
            lambda: resolveAdRewardConfirmationSelector(page, arguments.adRewardConfirmButtonXPath),
        ),
    }
    saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_ad_reward_confirmation_clicked", confirmationClickPayload)
    waitForRenderCycle(page)
    resetAdaptiveAdInterruptionRecoveryState("insufficientPackRecoveryFlowCompleted")
    print("[INFO] Insufficient-pack recovery flow completed; resuming pack opening.")
    return True



def clickAdRewardConfirmationIfPresent(
    page: Page,
    evidencePath: Path,
    arguments: argparse.Namespace,
    drawIndex: int,
    evidenceLabelPrefix: str,
) -> bool:
    confirmationResolution = resolveAdRewardConfirmationSelector(page, arguments.adRewardConfirmButtonXPath)
    if not confirmationResolution.get("ok"):
        return False

    saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_{evidenceLabelPrefix}_ad_reward_confirmation_target", confirmationResolution)
    if arguments.dryRun:
        print("[DRY-RUN] Ad-reward confirmation target resolved; confirmation click skipped.")
        return True

    confirmationClickPayload = {
        "confirmationResolution": confirmationResolution,
        **clickResolvedSelectorAndWait(
            page,
            confirmationResolution["selector"],
            lambda: resolveAdRewardConfirmationSelector(page, arguments.adRewardConfirmButtonXPath),
        ),
    }
    saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_{evidenceLabelPrefix}_ad_reward_confirmation_clicked", confirmationClickPayload)
    waitForRenderCycle(page)
    resetAdaptiveAdInterruptionRecoveryState("adRewardConfirmationClicked")
    print("[INFO] Ad-reward confirmation was clicked; resuming adaptive recovery.")
    return True


def recoverFromExpectedAdRecoveryOutcome(
    page: Page,
    evidencePath: Path,
    arguments: argparse.Namespace,
    drawIndex: int,
    reason: str,
) -> bool:
    saveEvidence(
        page,
        evidencePath,
        f"draw_{drawIndex:03d}_expected_ad_recovery_wait_started",
        {
            "reason": reason,
            "adRewardConfirmButtonXPath": arguments.adRewardConfirmButtonXPath,
            "adOverlayCloseButtonXPath": arguments.adOverlayCloseButtonXPath,
        },
    )
    print(
        "[INFO] Waiting for deferred ad close, reward-confirmation, or pack-page readiness "
        "before deciding that the run is complete."
    )
    adRecoveryOutcome = waitForAdRecoveryOutcomeTarget(
        page,
        arguments.adRewardConfirmButtonXPath,
        arguments.adOverlayCloseButtonXPath,
        arguments.remainingPackCountXPath,
        arguments.insufficientPackHeadingXPath,
        arguments.returnToPackPageXPath,
        arguments.recoverPackButtonXPath,
    )
    saveEvidence(
        page,
        evidencePath,
        f"draw_{drawIndex:03d}_expected_ad_recovery_outcome_detected",
        adRecoveryOutcome,
    )

    if adRecoveryOutcome.get("outcomeType") in {
        "remainingPackCountBecamePositive",
        "packTargetBecameAvailable",
        "insufficientPackRecoveryStillAvailable",
    }:
        resetAdaptiveAdInterruptionRecoveryState("deferredAdRecoveryReturnedControlToPackPage")
        print("[INFO] Deferred ad recovery returned control to the pack page; resuming pack opening.")
        return True

    if recoverFromAdInterruptionIfPresent(page, evidencePath, arguments, drawIndex):
        return True

    if clickAdRewardConfirmationIfPresent(
        page,
        evidencePath,
        arguments,
        drawIndex,
        "expected_deferred",
    ):
        return True

    missingOutcomePayload = {
        "reason": reason,
        "adInterruptionResolution": resolveAdInterruptionCloseTarget(page, arguments.adOverlayCloseButtonXPath),
        "adRewardConfirmationResolution": resolveAdRewardConfirmationSelector(page, arguments.adRewardConfirmButtonXPath),
    }
    saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_expected_ad_recovery_outcome_missing", missingOutcomePayload)
    raise WikiGachaAutomationError(
        "An ad-recovery outcome was expected and the adaptive wait returned, but neither an ad-close control nor "
        "a reward-confirmation control could be resolved. Inspect the outcome evidence."
    )

def completePackOpening(
    page: Page,
    arguments: argparse.Namespace,
    evidencePath: Path,
    drawIndex: int,
    allowNoInitialTarget: bool,
    initialRemainingPackResolution: dict[str, Any],
    expectDeferredAdRecoveryOutcome: bool = False,
) -> bool:
    seenOpeningStateHashes: set[str] = set()
    openingStepIndex = 0
    hasClickedOpeningTarget = False
    shouldWaitForDeferredAdOutcome = expectDeferredAdRecoveryOutcome

    while True:
        if recoverFromAdInterruptionIfPresent(page, evidencePath, arguments, drawIndex):
            if arguments.dryRun:
                return True
            shouldWaitForDeferredAdOutcome = True
            seenOpeningStateHashes.clear()
            waitForRenderCycle(page)
            continue

        if recoverFromInsufficientPackIfPresent(page, evidencePath, arguments, drawIndex):
            if arguments.dryRun:
                return True
            seenOpeningStateHashes.clear()
            waitForRenderCycle(page)

        returnResolution = resolveReturnToPackPageSelector(page, arguments.returnToPackPageXPath)
        if returnResolution.get("ok"):
            saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_return_to_pack_target", returnResolution)
            if arguments.dryRun:
                print("[DRY-RUN] Return-to-pack-page target resolved; return click skipped.")
                return True
            returnPayload = {
                "returnResolution": returnResolution,
                **clickResolvedSelectorAndWait(
                    page,
                    returnResolution["selector"],
                    lambda: resolveReturnToPackPageSelector(page, arguments.returnToPackPageXPath),
                ),
            }
            if not returnPayload.get("stateChanged"):
                saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_return_no_change_after_click", returnPayload)
                raise WikiGachaAutomationError(
                    "Return-to-pack-page button was clicked, but neither DOM nor rendered page state changed. "
                    "Inspect the return target evidence JSON/screenshot."
                )
            resetAdaptiveAdInterruptionRecoveryState("returnedToPackPage")
            saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_returned_to_pack_page", returnPayload)
            return True

        openingStateHash = buildShortHash(getPageStateFingerprint(page))
        if openingStateHash in seenOpeningStateHashes:
            repeatPayload = {
                "reason": "Opening flow reached a repeated rendered page state before the return-to-pack-page button appeared.",
                "openingStateHash": openingStateHash,
                "returnResolution": returnResolution,
            }
            saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_opening_repeated_state", repeatPayload)
            raise WikiGachaAutomationError(repeatPayload["reason"])
        seenOpeningStateHashes.add(openingStateHash)

        openingStepIndex += 1
        print(
            "[INFO] Resolving pack/card continuation target for draw "
            f"{formatDrawProgress(drawIndex, arguments.drawCount)}, step {openingStepIndex}"
        )
        targetResolution = resolveDrawTargetSelector(page, arguments.returnToPackPageXPath)
        if not targetResolution.get("ok"):
            recoveredGates = recoverFromPossibleEntryGate(
                page,
                evidencePath,
                rememberDismissal=not arguments.keepEntryNotices,
            )
            if recoveredGates:
                print(f"[INFO] Recovered from entry/update gate count: {len(recoveredGates)}")
                waitForRenderCycle(page)
                targetResolution = resolveDrawTargetSelector(page, arguments.returnToPackPageXPath)

        if not targetResolution.get("ok"):
            recoveredInsufficientPack = recoverFromInsufficientPackIfPresent(page, evidencePath, arguments, drawIndex)
            if recoveredInsufficientPack:
                if arguments.dryRun:
                    return True
                seenOpeningStateHashes.clear()
                waitForRenderCycle(page)
                targetResolution = resolveDrawTargetSelector(page, arguments.returnToPackPageXPath)

        if not targetResolution.get("ok"):
            recoveredAdInterruption = recoverFromAdInterruptionIfPresent(page, evidencePath, arguments, drawIndex)
            if recoveredAdInterruption:
                if arguments.dryRun:
                    return True
                shouldWaitForDeferredAdOutcome = True
                seenOpeningStateHashes.clear()
                waitForRenderCycle(page)
                continue

            if allowNoInitialTarget and not hasClickedOpeningTarget and shouldWaitForDeferredAdOutcome:
                recoveredExpectedAdOutcome = recoverFromExpectedAdRecoveryOutcome(
                    page,
                    evidencePath,
                    arguments,
                    drawIndex,
                    reason=(
                        "No pack/card target was available immediately after an ad-interruption close; "
                        "the rewarded-ad close or reward-confirmation control may still be deferred."
                    ),
                )
                if recoveredExpectedAdOutcome:
                    if arguments.dryRun:
                        return True
                    shouldWaitForDeferredAdOutcome = False
                    seenOpeningStateHashes.clear()
                    waitForRenderCycle(page)
                    continue

            refreshedReturnResolution = resolveReturnToPackPageSelector(page, arguments.returnToPackPageXPath)
            if refreshedReturnResolution.get("ok"):
                saveEvidence(
                    page,
                    evidencePath,
                    f"draw_{drawIndex:03d}_return_to_pack_after_target_suppressed",
                    {
                        "targetResolution": targetResolution,
                        "returnResolution": refreshedReturnResolution,
                    },
                )
                if arguments.dryRun:
                    return True
                returnPayload = {
                    "returnResolution": refreshedReturnResolution,
                    "targetResolutionBeforeReturn": targetResolution,
                    **clickResolvedSelectorAndWait(
                        page,
                        refreshedReturnResolution["selector"],
                        lambda: resolveReturnToPackPageSelector(page, arguments.returnToPackPageXPath),
                    ),
                }
                if not returnPayload.get("stateChanged"):
                    saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_return_after_target_suppressed_no_change", returnPayload)
                    raise WikiGachaAutomationError(
                        "Result-page draw targets were suppressed and the return-to-pack button was clicked, "
                        "but neither DOM nor rendered page state changed."
                    )
                resetAdaptiveAdInterruptionRecoveryState("returnedToPackPage")
                saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_returned_to_pack_page", returnPayload)
                return True

            noTargetPayload = {
                "targetResolution": targetResolution,
                "returnResolution": returnResolution,
                "refreshedReturnResolution": refreshedReturnResolution,
                "openingStateHash": openingStateHash,
                "hasClickedOpeningTarget": hasClickedOpeningTarget,
                "allowNoInitialTarget": allowNoInitialTarget,
                "initialRemainingPackResolution": initialRemainingPackResolution,
            }
            if allowNoInitialTarget and not hasClickedOpeningTarget:
                saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_no_pack_target_available", noTargetPayload)
                print("[INFO] No further pack target was detected; adaptive run is complete.")
                return False
            saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_opening_{openingStepIndex:03d}_no_target", noTargetPayload)
            raise WikiGachaAutomationError(
                targetResolution.get("reason", "No pack/card continuation target was found before the return button appeared.")
            )

        saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_opening_{openingStepIndex:03d}_target", targetResolution)
        if arguments.dryRun:
            print("[DRY-RUN] Pack/card continuation target resolved; click skipped.")
            return True

        progressPayload = {
            "targetResolution": targetResolution,
            "returnResolutionBeforeClick": returnResolution,
            **clickResolvedSelectorAndWait(
                page,
                targetResolution["selector"],
                lambda: resolveDrawTargetSelector(page, arguments.returnToPackPageXPath),
                returnOnRefreshResolutionFailure=True,
            ),
        }
        hasClickedOpeningTarget = True
        if not progressPayload.get("stateChanged"):
            returnResolutionAfterNoChange = resolveReturnToPackPageSelector(page, arguments.returnToPackPageXPath)
            progressPayload["returnResolutionAfterNoChange"] = returnResolutionAfterNoChange
            if returnResolutionAfterNoChange.get("ok"):
                recoveryEvidenceLabel = "aborted_target_return_recovered" if progressPayload.get("clickAborted") else "no_change_return_recovered"
                saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_opening_{openingStepIndex:03d}_{recoveryEvidenceLabel}", progressPayload)
                returnPayload = {
                    "returnResolution": returnResolutionAfterNoChange,
                    "noChangeTargetPayload": progressPayload,
                    **clickResolvedSelectorAndWait(
                        page,
                        returnResolutionAfterNoChange["selector"],
                        lambda: resolveReturnToPackPageSelector(page, arguments.returnToPackPageXPath),
                    ),
                }
                if not returnPayload.get("stateChanged"):
                    saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_return_after_no_change_target_no_change", returnPayload)
                    raise WikiGachaAutomationError(
                        "A stale or already-consumed continuation target produced no state change; "
                        "a return-to-pack button was found, but clicking it also produced no state change."
                    )
                resetAdaptiveAdInterruptionRecoveryState("returnedToPackPage")
                saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_returned_to_pack_page", returnPayload)
                return True

            refreshedTargetResolution = resolveDrawTargetSelector(page, arguments.returnToPackPageXPath)
            progressPayload["refreshedTargetResolutionAfterNoChange"] = refreshedTargetResolution
            saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_opening_{openingStepIndex:03d}_no_change_after_click", progressPayload)
            raise WikiGachaAutomationError(
                "Pack/card continuation target could not make progress. "
                "The most likely upstream cause is that the originally resolved target became stale and the fresh resolver either suppressed "
                "result-page controls or found no unrevealed pack target. Inspect clickAttempts, refreshResolutionFailure, "
                "refreshedTargetResolutionAfterNoChange, and returnResolutionAfterNoChange evidence."
            )
        resetAdaptiveAdInterruptionRecoveryState("packOpeningTargetAdvanced")
        saveEvidence(page, evidencePath, f"draw_{drawIndex:03d}_opening_{openingStepIndex:03d}_result", progressPayload)

def recoverFromPossibleEntryGate(page: Page, evidencePath: Path, rememberDismissal: bool) -> list[dict[str, Any]]:
    gateResolution = resolveEntryGateActionSelector(page)
    saveEvidence(page, evidencePath, "draw_blocking_layer_diagnostics", gateResolution)
    if not gateResolution.get("ok"):
        return []
    return dismissEntryGates(page, evidencePath, rememberDismissal=rememberDismissal)


def getExternalChromeExecutableCandidates(arguments: argparse.Namespace) -> list[Path]:
    candidateTexts: list[str] = []
    if arguments.externalChromePath:
        candidateTexts.append(arguments.externalChromePath)

    pathCandidates = [
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    candidateTexts.extend(candidate for candidate in pathCandidates if candidate)

    programFiles = [
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    ]
    windowsChromeSuffixes = [
        Path("Google") / "Chrome" / "Application" / "chrome.exe",
        Path("Google") / "Chrome Beta" / "Application" / "chrome.exe",
        Path("Google") / "Chrome Dev" / "Application" / "chrome.exe",
        Path("Google") / "Chrome SxS" / "Application" / "chrome.exe",
    ]
    for basePathText in programFiles:
        if not basePathText:
            continue
        basePath = Path(basePathText)
        candidateTexts.extend(str(basePath / suffix) for suffix in windowsChromeSuffixes)

    candidateTexts.extend(
        [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
            "/Applications/Google Chrome Dev.app/Contents/MacOS/Google Chrome Dev",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]
    )

    candidates: list[Path] = []
    seenCandidates: set[str] = set()
    for candidateText in candidateTexts:
        candidatePath = Path(candidateText).expanduser()
        normalizedCandidate = str(candidatePath.resolve()) if candidatePath.exists() else str(candidatePath)
        if normalizedCandidate in seenCandidates:
            continue
        seenCandidates.add(normalizedCandidate)
        candidates.append(candidatePath)
    return candidates


def resolveExternalChromeExecutable(arguments: argparse.Namespace) -> Path:
    candidates = getExternalChromeExecutableCandidates(arguments)
    for candidatePath in candidates:
        if candidatePath.is_file():
            return candidatePath
    searchedLocations = "\n".join(f"  - {candidate}" for candidate in candidates)
    raise WikiGachaAutomationError(
        "找不到可用的一般 Google Chrome / Chromium 執行檔，因此無法啟動 Manual/setup 登入模式。\n"
        "請安裝 Chrome，或用 --externalChromePath 指定 chrome.exe / Google Chrome 執行檔位置。\n"
        f"已搜尋位置：\n{searchedLocations}"
    )


def buildExternalChromeCommand(
    chromeExecutablePath: Path,
    profilePath: Path,
    targetUrl: str,
) -> list[str]:
    return [
        str(chromeExecutablePath),
        f"--user-data-dir={profilePath.resolve()}",
        "--profile-directory=Default",
        "--no-first-run",
        "--new-window",
        targetUrl,
    ]


def launchExternalChromeForManualControl(
    arguments: argparse.Namespace,
    profilePath: Path,
    targetUrl: str,
    modeLabel: str,
) -> subprocess.Popen[Any]:
    chromeExecutablePath = resolveExternalChromeExecutable(arguments)
    command = buildExternalChromeCommand(chromeExecutablePath, profilePath, targetUrl)
    print(f"[INFO] {modeLabel} 使用一般瀏覽器，不經由 Playwright 控制：{chromeExecutablePath}")
    print(f"[INFO] 使用同一個持久化 profile：{profilePath.resolve()}")
    print("[INFO] 這個模式不會出現『Chrome 目前受到自動測試軟體控制』，適合手動 Google 登入與伺服器同步。")
    return subprocess.Popen(command)


def runExternalManualMode(arguments: argparse.Namespace, profilePath: Path) -> None:
    print("[INFO] Manual mode selected. The script will launch ordinary Chrome for manual control.")
    browserProcess = launchExternalChromeForManualControl(arguments, profilePath, arguments.url, "Manual 模式")
    if sys.stdin.isatty():
        print("[INFO] 請在開啟的一般 Chrome 視窗中操作 WikiGacha、登入 Google、啟用伺服器同步。")
        print("[INFO] 完成後請先關閉 Chrome 視窗，再回到此終端機按 Enter。")
        input("[INFO] Manual 模式已啟動；按 Enter 結束等待：")
        if browserProcess.poll() is None:
            print("[INFO] 一般 Chrome 仍在執行；腳本不會強制關閉它，以免中斷登入狀態寫入。")
    else:
        print("[INFO] stdin 非互動模式；已啟動一般 Chrome 後直接返回。")


def runExternalGoogleSignInSetup(arguments: argparse.Namespace, profilePath: Path) -> None:
    print("[SETUP] Google 登入／伺服器同步設定模式將使用一般 Chrome，而不是自動化控制中的瀏覽器。")
    browserProcess = launchExternalChromeForManualControl(arguments, profilePath, arguments.url, "Setup 模式")
    if sys.stdin.isatty():
        print("[SETUP] 請在一般 Chrome 內點『伺服器同步』並完成 Google 登入。")
        print("[SETUP] 成功登入後，請先關閉一般 Chrome 視窗，讓 profile 狀態完整寫入磁碟。")
        input("[SETUP] 完成後回到終端機按 Enter；後續 Bot 模式會沿用這個 profile：")
        if browserProcess.poll() is None:
            print("[SETUP] 一般 Chrome 仍在執行；腳本不會強制關閉它。請確認關閉後再啟動 Bot，避免 profile 被鎖定。")
    else:
        print("[SETUP] stdin 非互動模式；已啟動一般 Chrome 後直接返回。")


def performDraws(page: Page, arguments: argparse.Namespace, evidencePath: Path) -> None:
    resetAdaptiveAdInterruptionRecoveryState("performDrawsStarted")
    page.goto(arguments.url, wait_until="domcontentloaded", timeout=0)
    waitForPageReady(page)
    dismissedGates = dismissEntryGates(page, evidencePath, rememberDismissal=not arguments.keepEntryNotices)
    initialRemainingPackResolution = resolveRemainingPackCount(
        page,
        arguments.remainingPackCountXPath,
        arguments.insufficientPackHeadingXPath,
    )
    saveEvidence(
        page,
        evidencePath,
        "before_draw",
        {
            "drawCount": arguments.drawCount,
            "drawRunMode": getDrawRunMode(arguments),
            "dismissedGates": dismissedGates,
            "returnToPackPageXPath": arguments.returnToPackPageXPath,
            "remainingPackCountXPath": arguments.remainingPackCountXPath,
            "insufficientPackHeadingXPath": arguments.insufficientPackHeadingXPath,
            "recoverPackButtonXPath": arguments.recoverPackButtonXPath,
            "adRewardConfirmButtonXPath": arguments.adRewardConfirmButtonXPath,
            "adOverlayCloseButtonXPath": arguments.adOverlayCloseButtonXPath,
            "remainingPackResolution": initialRemainingPackResolution,
        },
    )

    completedDrawCount = 0
    drawIndex = 1
    while arguments.drawCount is None or drawIndex <= arguments.drawCount:
        remainingPackResolutionBeforeDraw = resolveRemainingPackCount(
            page,
            arguments.remainingPackCountXPath,
            arguments.insufficientPackHeadingXPath,
        )
        remainingPacksBeforeDraw = hasRemainingPacks(remainingPackResolutionBeforeDraw)
        expectDeferredAdRecoveryOutcome = False
        saveEvidence(
            page,
            evidencePath,
            f"draw_{drawIndex:03d}_remaining_pack_count_before",
            {
                "completedDrawCount": completedDrawCount,
                "drawRunMode": getDrawRunMode(arguments),
                "remainingPackResolution": remainingPackResolutionBeforeDraw,
            },
        )

        if remainingPacksBeforeDraw is False:
            recoveredAdInterruption = recoverFromAdInterruptionIfPresent(page, evidencePath, arguments, drawIndex)
            if recoveredAdInterruption:
                expectDeferredAdRecoveryOutcome = True
                if arguments.dryRun:
                    saveEvidence(
                        page,
                        evidencePath,
                        "dry_run_completed_after_ad_interruption_close_resolution",
                        {
                            "completedDrawCount": completedDrawCount,
                            "nextDrawIndex": drawIndex,
                            "drawRunMode": getDrawRunMode(arguments),
                            "remainingPackResolution": remainingPackResolutionBeforeDraw,
                        },
                    )
                    break
                remainingPackResolutionBeforeDraw = resolveRemainingPackCount(
                    page,
                    arguments.remainingPackCountXPath,
                    arguments.insufficientPackHeadingXPath,
                )
                remainingPacksBeforeDraw = hasRemainingPacks(remainingPackResolutionBeforeDraw)
                saveEvidence(
                    page,
                    evidencePath,
                    f"draw_{drawIndex:03d}_remaining_pack_count_after_ad_interruption_close",
                    {
                        "completedDrawCount": completedDrawCount,
                        "drawRunMode": getDrawRunMode(arguments),
                        "remainingPackResolution": remainingPackResolutionBeforeDraw,
                    },
                )

            recoveredInsufficientPack = recoverFromInsufficientPackIfPresent(page, evidencePath, arguments, drawIndex)
            if recoveredInsufficientPack:
                expectDeferredAdRecoveryOutcome = True
                if arguments.dryRun:
                    saveEvidence(
                        page,
                        evidencePath,
                        "dry_run_completed_after_insufficient_pack_recovery_resolution",
                        {
                            "completedDrawCount": completedDrawCount,
                            "nextDrawIndex": drawIndex,
                            "drawRunMode": getDrawRunMode(arguments),
                            "remainingPackResolution": remainingPackResolutionBeforeDraw,
                        },
                    )
                    break
                remainingPackResolutionBeforeDraw = resolveRemainingPackCount(
                    page,
                    arguments.remainingPackCountXPath,
                    arguments.insufficientPackHeadingXPath,
                )
                remainingPacksBeforeDraw = hasRemainingPacks(remainingPackResolutionBeforeDraw)
                saveEvidence(
                    page,
                    evidencePath,
                    f"draw_{drawIndex:03d}_remaining_pack_count_after_recovery",
                    {
                        "completedDrawCount": completedDrawCount,
                        "drawRunMode": getDrawRunMode(arguments),
                        "remainingPackResolution": remainingPackResolutionBeforeDraw,
                    },
                )

            if remainingPacksBeforeDraw is False:
                saveEvidence(
                    page,
                    evidencePath,
                    "remaining_pack_counter_zero_before_ui_probe",
                    {
                        "completedDrawCount": completedDrawCount,
                        "nextDrawIndex": drawIndex,
                        "drawRunMode": getDrawRunMode(arguments),
                        "remainingPackResolution": remainingPackResolutionBeforeDraw,
                        "reason": (
                            "The visible remaining-pack counter is zero, but the page itself may still expose "
                            "a pack target that opens the insufficient-pack recovery flow. The automation will "
                            "probe that UI path before deciding that the adaptive run is complete."
                        ),
                    },
                )
                print(
                    "[INFO] Remaining pack counter is zero; probing the UI for a pack target "
                    "or ad-recovery path before stopping."
                )

        allowNoInitialTarget = remainingPacksBeforeDraw is not True
        print(
            "[INFO] Opening pack lifecycle for draw "
            f"{formatDrawProgress(drawIndex, arguments.drawCount)} "
            f"with remainingPackCount={getRemainingPackCountValue(remainingPackResolutionBeforeDraw)}"
        )
        openedPack = completePackOpening(
            page,
            arguments,
            evidencePath,
            drawIndex,
            allowNoInitialTarget=allowNoInitialTarget,
            initialRemainingPackResolution=remainingPackResolutionBeforeDraw,
            expectDeferredAdRecoveryOutcome=expectDeferredAdRecoveryOutcome,
        )
        remainingPackResolutionAfterDraw = resolveRemainingPackCount(
            page,
            arguments.remainingPackCountXPath,
            arguments.insufficientPackHeadingXPath,
        )
        saveEvidence(
            page,
            evidencePath,
            f"draw_{drawIndex:03d}_remaining_pack_count_after",
            {
                "completedDrawCountBeforeAccounting": completedDrawCount,
                "openedPack": openedPack,
                "drawRunMode": getDrawRunMode(arguments),
                "remainingPackResolutionBeforeDraw": remainingPackResolutionBeforeDraw,
                "remainingPackResolutionAfterDraw": remainingPackResolutionAfterDraw,
            },
        )

        if not openedPack:
            saveEvidence(
                page,
                evidencePath,
                "adaptive_draws_completed",
                {
                    "completedDrawCount": completedDrawCount,
                    "lastAttemptedDrawIndex": drawIndex,
                    "drawRunMode": getDrawRunMode(arguments),
                    "remainingPackResolution": remainingPackResolutionAfterDraw,
                },
            )
            break

        completedDrawCount += 1
        if arguments.dryRun:
            saveEvidence(
                page,
                evidencePath,
                "dry_run_completed_after_first_resolution",
                {
                    "completedDrawCount": completedDrawCount,
                    "lastAttemptedDrawIndex": drawIndex,
                    "drawRunMode": getDrawRunMode(arguments),
                    "remainingPackResolution": remainingPackResolutionAfterDraw,
                },
            )
            break

        dismissedGatesAfterReturn = dismissEntryGates(
            page,
            evidencePath,
            rememberDismissal=not arguments.keepEntryNotices,
        )
        if dismissedGatesAfterReturn:
            saveEvidence(
                page,
                evidencePath,
                f"draw_{drawIndex:03d}_post_return_gate_dismissed",
                {"dismissedGates": dismissedGatesAfterReturn},
            )
        drawIndex += 1

    finalRemainingPackResolution = resolveRemainingPackCount(
        page,
        arguments.remainingPackCountXPath,
        arguments.insufficientPackHeadingXPath,
    )
    saveEvidence(
        page,
        evidencePath,
        "after_draws",
        {
            "completedDrawCount": completedDrawCount,
            "nextDrawIndex": drawIndex,
            "drawRunMode": getDrawRunMode(arguments),
            "drawCount": arguments.drawCount,
            "remainingPackCountXPath": arguments.remainingPackCountXPath,
            "insufficientPackHeadingXPath": arguments.insufficientPackHeadingXPath,
            "recoverPackButtonXPath": arguments.recoverPackButtonXPath,
            "adRewardConfirmButtonXPath": arguments.adRewardConfirmButtonXPath,
            "adOverlayCloseButtonXPath": arguments.adOverlayCloseButtonXPath,
            "remainingPackResolution": finalRemainingPackResolution,
        },
    )

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


def isBrowserLifecycleClosedError(error: BaseException) -> bool:
    errorText = " ".join(
        [
            type(error).__name__,
            str(error),
            repr(error),
        ]
    ).lower()
    browserClosedSignals = (
        "target page, context or browser has been closed",
        "browser has been closed",
        "browser closed",
        "target closed",
        "page closed",
        "context closed",
        "browser disconnected",
        "connection closed",
        "connection terminated",
        "transport closed",
        "websocket closed",
        "socket is closed",
        "closed before",
        "object has been collected",
    )
    return isinstance(error, PlaywrightError) and any(signal in errorText for signal in browserClosedSignals)


def closePageQuietly(page: Page | None) -> None:
    if page is None or page.is_closed():
        return
    try:
        page.close(run_before_unload=False)
    except PlaywrightError as error:
        if not isBrowserLifecycleClosedError(error):
            print(
                f"[WARN] Browser page close raised {type(error).__name__}: {error}",
                file=sys.stderr,
            )


def closeContextQuietly(context: BrowserContext | None) -> None:
    if context is None:
        return
    try:
        context.close()
    except PlaywrightError as error:
        if not isBrowserLifecycleClosedError(error):
            print(
                f"[WARN] Browser context close raised {type(error).__name__}: {error}",
                file=sys.stderr,
            )


def saveErrorEvidenceIfPossible(
    page: Page | None,
    evidencePath: Path,
    error: BaseException,
    arguments: argparse.Namespace,
) -> None:
    if page is None or page.is_closed():
        return
    try:
        saveErrorEvidence(page, evidencePath, error, arguments)
    except Exception as evidenceError:
        print(
            "[WARN] Failed to persist error evidence: "
            f"{type(evidenceError).__name__}: {evidenceError}",
            file=sys.stderr,
        )


def waitForBotLifecycleClosure(page: Page | None, context: BrowserContext | None) -> None:
    if page is not None and not page.is_closed():
        print("[INFO] Bot lifecycle completed; keeping the supervisor alive until the browser is closed or Ctrl+C is pressed.")
        try:
            page.wait_for_event("close", timeout=0)
            return
        except PlaywrightError as error:
            if isBrowserLifecycleClosedError(error):
                return
            raise
    if context is not None:
        print("[INFO] Bot lifecycle completed; waiting for the browser context to close or Ctrl+C.")
        try:
            context.wait_for_event("close", timeout=0)
            return
        except PlaywrightError as error:
            if isBrowserLifecycleClosedError(error):
                return
            raise


def buildAdaptiveCompletionRestartPayload(page: Page, arguments: argparse.Namespace) -> dict[str, Any]:
    renderedStateFingerprint = getRenderedStateFingerprint(page)
    return {
        "reason": (
            "The unbounded adaptive draw run reached its page-observed completion state. "
            "The current automated Chrome page/context will be closed and a fresh bot lifecycle will be started."
        ),
        "url": page.url,
        "drawRunMode": getDrawRunMode(arguments),
        "profileDir": arguments.profileDir,
        "pageStateFingerprintHash": buildShortHash(getPageStateFingerprint(page)),
        "renderedStateFingerprintHash": buildShortHash(renderedStateFingerprint),
        "routineEvidencePersisted": saveRoutineEvidence,
        "evidenceEventTrail": evidenceEventTrail,
    }


def requestBotLifecycleRestartAfterAdaptiveCompletion(
    page: Page | None,
    context: BrowserContext | None,
    evidencePath: Path,
    arguments: argparse.Namespace,
) -> None:
    if arguments.keepBrowserOpenAfterAdaptiveCompletion:
        waitForBotLifecycleClosure(page, context)
        return

    if page is not None and not page.is_closed():
        restartPayload = buildAdaptiveCompletionRestartPayload(page, arguments)
        saveEvidence(page, evidencePath, "adaptive_completion_lifecycle_restart_requested", restartPayload)

    print(
        "[INFO] Bot lifecycle completed; closing the current automated Chrome page/context "
        "and restarting from a fresh browser lifecycle."
    )
    closePageQuietly(page)


def runBotLifecycle(
    playwright: Any,
    arguments: argparse.Namespace,
    profilePath: Path,
    evidencePath: Path,
    botLifecycleIndex: int,
) -> bool:
    context: BrowserContext | None = None
    page: Page | None = None
    resetEvidenceEventTrail()
    print(f"[INFO] Starting Bot lifecycle #{botLifecycleIndex} from a fresh browser context.")
    try:
        context = launchPersistentContext(playwright, arguments, profilePath)
        page = context.pages[0] if context.pages else context.new_page()
        performDraws(page, arguments, evidencePath)
        if arguments.drawCount is not None:
            return False
        requestBotLifecycleRestartAfterAdaptiveCompletion(page, context, evidencePath, arguments)
        return True
    except KeyboardInterrupt:
        raise
    except BrowserLifecycleRestartRequired as restartRequest:
        print(
            "[WARN] Adaptive ad-interruption recovery requested a browser lifecycle restart: "
            f"{restartRequest}",
            file=sys.stderr,
        )
        closePageQuietly(page)
        return True
    except Exception as error:
        if isBrowserLifecycleClosedError(error):
            print(
                "[WARN] Bot lifecycle browser/page/context was closed unexpectedly; "
                "starting a new lifecycle from the beginning.",
                file=sys.stderr,
            )
            return True
        saveErrorEvidenceIfPossible(page, evidencePath, error, arguments)
        raise
    finally:
        closeContextQuietly(context)


def runSupervisedBotMode(arguments: argparse.Namespace, profilePath: Path, evidencePath: Path) -> int:
    botLifecycleIndex = 1
    with sync_playwright() as playwright:
        while True:
            shouldRestartLifecycle = runBotLifecycle(
                playwright,
                arguments,
                profilePath,
                evidencePath,
                botLifecycleIndex,
            )
            if not shouldRestartLifecycle:
                return 0
            botLifecycleIndex += 1


def main() -> int:
    arguments = parseArguments()
    ensureArgumentsAreValid(arguments)
    executionMode = resolveExecutionMode(arguments)
    arguments.executionMode = executionMode
    print(f"[INFO] Execution mode: {executionMode}")
    setRoutineEvidenceEnabled(arguments.saveRoutineEvidence)
    evidencePath = createEvidenceDirectory(
        arguments.evidenceDir,
        shouldCreateImmediately=arguments.saveRoutineEvidence,
    )
    profilePath = Path(arguments.profileDir)
    profilePath.mkdir(parents=True, exist_ok=True)

    if arguments.setup:
        runExternalGoogleSignInSetup(arguments, profilePath)
        return 0

    if arguments.executionMode == "manual":
        runExternalManualMode(arguments, profilePath)
        return 0

    return runSupervisedBotMode(arguments, profilePath, evidencePath)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user.", file=sys.stderr)
        raise SystemExit(130)
