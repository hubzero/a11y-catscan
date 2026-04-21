/**
 * alfa-engine.mjs — Siteimprove Alfa engine + Playwright browser server.
 *
 * This subprocess serves two roles:
 *   1. Launches Chromium via Playwright's launchServer() and provides
 *      a GUID-protected WebSocket endpoint for the Python scanner.
 *   2. Runs Alfa accessibility rules on pages by navigating to them
 *      in its own browser context (with cookies from Python for auth).
 *
 * Security: The WebSocket endpoint includes a random GUID — only
 * clients that know the full URL can connect.  No open debug ports.
 *
 * Protocol:
 *   Init (stdin):   {"level": "aa", "args": [...]}
 *   Ready (stdout): {"ok": true, "wsEndpoint": "ws://...", "rules": N}
 *   Scan (stdin):   {"pageId": "URL", "cookies": [{name,value,domain,path}]}
 *   Result (stdout): {"ok": true, "violations": [...], "incomplete": [...]}
 *   Quit (stdin):   {"quit": true}
 */

import { Playwright } from "@siteimprove/alfa-playwright";
import { Audit } from "@siteimprove/alfa-act";
import { Rules } from "@siteimprove/alfa-rules";
import { chromium } from "playwright";
import readline from "readline";

// WCAG success criteria → level mapping
const WCAG_LEVELS = {
  "1.1.1": "a",
  "1.2.1": "a", "1.2.2": "a", "1.2.3": "a",
  "1.2.4": "aa", "1.2.5": "aa",
  "1.2.6": "aaa", "1.2.7": "aaa", "1.2.8": "aaa", "1.2.9": "aaa",
  "1.3.1": "a", "1.3.2": "a", "1.3.3": "a",
  "1.3.4": "aa", "1.3.5": "aa",
  "1.3.6": "aaa",
  "1.4.1": "a", "1.4.2": "a",
  "1.4.3": "aa", "1.4.4": "aa", "1.4.5": "aa",
  "1.4.6": "aaa", "1.4.7": "aaa", "1.4.8": "aaa", "1.4.9": "aaa",
  "1.4.10": "aa", "1.4.11": "aa", "1.4.12": "aa", "1.4.13": "aa",
  "2.1.1": "a", "2.1.2": "a",
  "2.1.3": "aaa", "2.1.4": "a",
  "2.2.1": "a", "2.2.2": "a",
  "2.2.3": "aaa", "2.2.4": "aaa", "2.2.5": "aaa", "2.2.6": "aaa",
  "2.3.1": "a",
  "2.3.2": "aaa", "2.3.3": "aaa",
  "2.4.1": "a", "2.4.2": "a", "2.4.3": "a", "2.4.4": "a",
  "2.4.5": "aa", "2.4.6": "aa", "2.4.7": "aa",
  "2.4.8": "aaa", "2.4.9": "aaa", "2.4.10": "aaa",
  "2.4.11": "aa", "2.4.12": "aaa", "2.4.13": "aaa",
  "2.5.1": "a", "2.5.2": "a", "2.5.3": "a", "2.5.4": "a",
  "2.5.5": "aaa", "2.5.6": "aaa",
  "2.5.7": "aa", "2.5.8": "aa",
  "3.1.1": "a",
  "3.1.2": "aa",
  "3.1.3": "aaa", "3.1.4": "aaa", "3.1.5": "aaa", "3.1.6": "aaa",
  "3.2.1": "a", "3.2.2": "a",
  "3.2.3": "aa", "3.2.4": "aa",
  "3.2.5": "aaa", "3.2.6": "a",
  "3.3.1": "a", "3.3.2": "a",
  "3.3.3": "aa", "3.3.4": "aa",
  "3.3.5": "aaa", "3.3.6": "aaa",
  "3.3.7": "a", "3.3.8": "aa", "3.3.9": "a",
  "4.1.1": "a", "4.1.2": "a", "4.1.3": "aa",
};

const LEVEL_INCLUDES = {
  a: new Set(["a"]),
  aa: new Set(["a", "aa"]),
  aaa: new Set(["a", "aa", "aaa"]),
};

function filterRulesByLevel(rules, level) {
  const allowed = LEVEL_INCLUDES[level] || LEVEL_INCLUDES["aa"];
  return rules.filter((rule) => {
    const reqs = rule.toJSON().requirements || [];
    const wcag = reqs.filter((r) => r.type === "criterion");
    if (wcag.length === 0) return true;
    return wcag.some((r) => {
      const scLevel = WCAG_LEVELS[r.chapter];
      return scLevel && allowed.has(scLevel);
    });
  });
}

function normalizeOutcome(j) {
  const rule_uri = j.rule?.uri || "";
  const rule_id = rule_uri.split("/").pop() || rule_uri;
  const wcag = (j.rule?.requirements || [])
    .filter((r) => r.type === "criterion")
    .map((r) => r.chapter);

  let target = "";
  if (j.target?.name) {
    target = j.target.name;
    // Add id if present
    if (j.target.attributes) {
      for (const attr of j.target.attributes) {
        if (attr.name === "id" && attr.value) {
          target = "#" + attr.value;
          break;
        }
      }
      // Fall back to class if no id
      if (!target.startsWith("#")) {
        for (const attr of j.target.attributes) {
          if (attr.name === "class" && attr.value) {
            target += "." + attr.value.split(" ").join(".");
            break;
          }
        }
      }
    }
  }
  // Reconstruct a minimal HTML snippet from the target
  let html = "";
  if (j.target?.name) {
    html = "<" + j.target.name;
    if (j.target.attributes) {
      for (const attr of j.target.attributes) {
        if (["id", "class", "role", "href", "src", "alt", "aria-label"].includes(attr.name) && attr.value) {
          html += " " + attr.name + '="' + attr.value.substring(0, 50) + '"';
        }
      }
    }
    html += ">";
  }

  let message = "";
  if (j.expectations) {
    try {
      for (const [_, exp] of j.expectations) {
        // "err" for failed outcomes, "cantTell" for uncertain ones
        if (exp.type === "err" && exp.error?.message) {
          message = exp.error.message;
          break;
        }
        if (exp.type === "cantTell" && exp.error?.message) {
          message = exp.error.message;
          break;
        }
        // Fallback: any message we can find
        if (!message && exp.message) {
          message = exp.message;
        }
      }
    } catch {
      message = JSON.stringify(j.expectations).substring(0, 200);
    }
  }

  return { rule: rule_id, uri: rule_uri, wcag, target, html: html || target, message };
}

let server = null;
let browser = null;
let filteredRules = null;

async function init(level, launchArgs) {
  const allRules = [...Rules].map(([_, rule]) => rule);
  filteredRules = filterRulesByLevel(allRules, level || "aa");

  // Launch Chromium via Playwright server.
  // The WS endpoint includes a random GUID — acts as bearer token.
  server = await chromium.launchServer({
    headless: true,
    args: launchArgs || ["--disable-dev-shm-usage", "--disable-gpu"],
  });

  // Connect to our own server for Alfa scanning
  browser = await chromium.connect(server.wsEndpoint());

  return server.wsEndpoint();
}

async function scanPage(pageId, cookies) {
  // Create a fresh context with auth cookies, navigate, scan, close.
  // Each scan gets its own context so cookies don't leak between scans
  // and the context is cleaned up after.
  const ctxOptions = {};
  const ctx = await browser.newContext(ctxOptions);

  // Set auth cookies if provided
  if (cookies && cookies.length > 0) {
    await ctx.addCookies(cookies);
  }

  const page = await ctx.newPage();
  try {
    await page.goto(pageId, { waitUntil: "networkidle", timeout: 60000 });

    const alfaPage = await Playwright.toPage(page);
    const outcomes = await Audit.of(alfaPage, filteredRules).evaluate();

    // Collect outcomes into arrays.  outcomes may be a single-use
    // iterator so we materialize everything in one pass.
    const violations = [];
    const incomplete = [];
    const rawTargets = [];  // parallel array for selector resolution
    let passes = 0;

    for (const outcome of outcomes) {
      const j = outcome.toJSON();
      const t = j.target || {};
      if (j.outcome === "failed") {
        violations.push(normalizeOutcome(j));
        rawTargets.push({
          type: t.type,
          name: t.name || null,
          text: t.data || null,
          attrs: (t.attributes || [])
            .filter((a) => a.value)
            .map((a) => ({ n: a.name, v: a.value })),
        });
      } else if (j.outcome === "cantTell") {
        incomplete.push(normalizeOutcome(j));
        rawTargets.push({
          type: t.type,
          name: t.name || null,
          text: t.data || null,
          attrs: (t.attributes || [])
            .filter((a) => a.value)
            .map((a) => ({ n: a.name, v: a.value })),
        });
      } else if (j.outcome === "passed") {
        passes++;
      }
    }

    // Resolve selectors in the live DOM while the page is still open.
    // rawTargets was built in parallel with violations/incomplete above.
    const allFindings = [...violations, ...incomplete];
    if (allFindings.length > 0) {
      try {
        const resolved = await page.evaluate((targets) => {
          function uniqueSelector(el) {
            if (!el || el === document) return "html";
            if (el.id) return "#" + CSS.escape(el.id);
            const path = [];
            let node = el;
            while (node && node.parentElement) {
              let seg = node.tagName.toLowerCase();
              const parent = node.parentElement;
              const sibs = Array.from(parent.children).filter(
                (c) => c.tagName === node.tagName
              );
              if (sibs.length > 1) {
                seg +=
                  ":nth-of-type(" + (sibs.indexOf(node) + 1) + ")";
              }
              if (node.id) {
                path.unshift("#" + CSS.escape(node.id));
                break;
              }
              path.unshift(seg);
              node = parent;
            }
            return path.join(" > ");
          }

          return targets.map((t) => {
            // Text nodes: find the parent element by text content
            if (t.type === "text" && t.text) {
              // Use TreeWalker to find the text node, return its parent
              const walker = document.createTreeWalker(
                document.body, NodeFilter.SHOW_TEXT);
              let node;
              while ((node = walker.nextNode())) {
                if (node.textContent.trim().startsWith(t.text.trim().substring(0, 20))) {
                  const el = node.parentElement;
                  if (el) return {
                    selector: uniqueSelector(el),
                    html: el.outerHTML.substring(0, 200),
                  };
                }
              }
              return null;
            }

            if (t.type !== "element" || !t.name) return null;

            // Try id first
            const idAttr = t.attrs.find((a) => a.n === "id");
            if (idAttr) {
              const el = document.getElementById(idAttr.v);
              if (el)
                return {
                  selector: uniqueSelector(el),
                  html: el.outerHTML.substring(0, 200),
                };
            }

            // Build an attribute selector from all available attrs
            let sel = t.name;
            for (const a of t.attrs) {
              if (["class", "id", "role", "href", "src", "name", "type",
                   "aria-label", "aria-labelledby", "for", "action",
                  ].includes(a.n)) {
                sel +=
                  "[" + a.n + "=" + JSON.stringify(a.v) + "]";
              }
            }

            try {
              const matches = document.querySelectorAll(sel);
              if (matches.length === 1) {
                return {
                  selector: uniqueSelector(matches[0]),
                  html: matches[0].outerHTML.substring(0, 200),
                };
              }
              if (matches.length > 1) {
                // Multiple matches — return the first with its
                // unique selector (nth-of-type will disambiguate)
                return {
                  selector: uniqueSelector(matches[0]),
                  html: matches[0].outerHTML.substring(0, 200),
                };
              }
            } catch {
              // Invalid selector — try just the tag
            }

            // Fallback: just the tag
            try {
              const el = document.querySelector(t.name);
              if (el)
                return {
                  selector: uniqueSelector(el),
                  html: el.outerHTML.substring(0, 200),
                };
            } catch {}

            return null;
          });
        }, rawTargets);

        for (let i = 0; i < allFindings.length; i++) {
          if (resolved[i]) {
            allFindings[i].target = resolved[i].selector;
            allFindings[i].html = resolved[i].html;
          }
        }
        // Debug: log what we attempted
        const textTargets = rawTargets.filter(t => t.type === 'text');
        if (textTargets.length > 0) {
          const matched = resolved.filter(r => r !== null).length;
          process.stderr.write(
            "Alfa resolve: " + resolved.length + " targets, " +
            matched + " resolved, " + textTargets.length +
            " text nodes (text samples: " +
            textTargets.slice(0, 3).map(t => JSON.stringify(t.text?.substring(0, 20))).join(", ") +
            ")\n");
        }
      } catch (resolveErr) {
        // Keep the original target/html if resolution fails
        process.stderr.write("Resolve error: " + resolveErr.message + "\n");
      }
    }

    return { ok: true, pageId, violations, incomplete, passes };
  } catch (e) {
    return { ok: false, pageId, error: e.message };
  } finally {
    await page.close();
    await ctx.close();
  }
}

// Main loop
const rl = readline.createInterface({ input: process.stdin });
let initialized = false;

for await (const line of rl) {
  try {
    const req = JSON.parse(line.trim());

    if (!initialized) {
      const args = req.args || ["--disable-dev-shm-usage", "--disable-gpu"];
      const wsEndpoint = await init(req.level || "aa", args);
      initialized = true;
      process.stdout.write(
        JSON.stringify({
          ok: true,
          ready: true,
          wsEndpoint,
          rules: filteredRules.length,
          level: req.level || "aa",
        }) + "\n"
      );
      continue;
    }

    if (req.pageId) {
      const result = await scanPage(req.pageId, req.cookies || []);
      process.stdout.write(JSON.stringify(result) + "\n");
    } else if (req.quit) {
      break;
    }
  } catch (e) {
    process.stdout.write(
      JSON.stringify({ ok: false, error: e.message }) + "\n"
    );
  }
}

// Shut down
if (browser) {
  try { await browser.close(); } catch {}
}
if (server) {
  try { await server.close(); } catch {}
}
