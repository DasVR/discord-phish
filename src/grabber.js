require('dotenv').config();
const fs = require('fs');
const path = require('path');
const {
  log,
  logError,
  launchBrowser,
  closeBrowser,
  createLoginTab,
  waitForAuthentication,
  bootstrapAppSession,
  waitForValidToken,
  finalizeCapture,
  QR_OUTPUT_PATH,
  TOKEN_LOG_PATH,
} = require('./grabber-core');

async function run() {
  let browser;

  try {
    browser = await launchBrowser();
    const tab = await createLoginTab(browser);

    const savedQrPath = path.join(QR_OUTPUT_PATH);
    fs.writeFileSync(savedQrPath, tab.qrBuffer);
    log('qr', `Saved QR image to ${savedQrPath}`);

    log('auth', 'Monitoring for authentication state changes');
    const authenticated = await waitForAuthentication(tab.page, tab.initialUrl);
    if (!authenticated) {
      throw new Error('Authentication was not detected before the QR code expired');
    }

    await bootstrapAppSession(tab.page);

    log('token', 'Waiting for a usable session token');
    const { token, user } = await waitForValidToken(tab.page, tab.sessionCapture);

    log('validate', `Token valid for user ${user.username} (${user.id})`);
    log('profile', `Email: ${user.email ?? 'N/A'} | MFA: ${user.mfa_enabled ? 'enabled' : 'disabled'}`);

    await finalizeCapture(token, user);
    log('log', `Token saved to ${TOKEN_LOG_PATH}`);
    log('done', 'Script completed successfully');
  } catch (error) {
    logError('run', 'Script failed', error);
    process.exitCode = 1;
  } finally {
    await closeBrowser(browser);
  }
}

run();
