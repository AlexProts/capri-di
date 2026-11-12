// Use only through an SSH-forwarded desktop. Never expose VNC/CDP publicly.
import { chromium } from 'playwright';
const allowed = new Set(['lms', 'time']);
const source = process.argv[2];
if (!allowed.has(source)) throw new Error('Choose lms or time');
const browser = await chromium.launchPersistentContext('/var/lib/capri-di/di/browser-profile', {headless:false});
const page = browser.pages()[0] || await browser.newPage();
await page.goto(source === 'lms' ? 'https://my.centraluniversity.ru/learn/courses/view/actual/all' : 'https://time.cu.ru/tsentralnyy-universitet/');
console.log('Sign in on the page. Close the browser when done. No credentials are printed.');
await new Promise(resolve => browser.on('close', resolve));
