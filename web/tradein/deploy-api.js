#!/usr/bin/env node
/**
 * Deploy tradein site to Cloudflare Pages via Direct Upload API.
 * Fallback when `wrangler pages deploy` fails (e.g. TTY issues).
 *
 * Usage: node deploy-api.js
 */
const https = require('https');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const ACCOUNT_ID = 'cf4198e651bf3009877d49f688c9d88e';
const PROJECT_NAME = 'tradein-buylist';
const DEPLOY_DIR = __dirname;

// Files to deploy (relative to DEPLOY_DIR)
const FILES = [
  '_headers',
  'admin/index.html',
  'css/styles.css',
  'data/buylist.json',
  'index.html',
  'js/api.js',
  'js/app.js',
  'js/cart.js',
  'js/set-names.js',
  'js/sku-parser.js',
];

function getToken() {
  const configPath = path.join(
    process.env.HOME, 'Library/Preferences/.wrangler/config/default.toml'
  );
  const toml = fs.readFileSync(configPath, 'utf8');
  return toml.match(/oauth_token = "([^"]+)"/)[1];
}

function sha256hex(buffer) {
  return crypto.createHash('sha256').update(buffer).digest('hex');
}

function apiRequest(method, apiPath, token, body, contentType) {
  return new Promise((resolve, reject) => {
    const opts = {
      hostname: 'api.cloudflare.com',
      path: apiPath,
      method,
      headers: { 'Authorization': 'Bearer ' + token },
    };
    if (contentType) opts.headers['Content-Type'] = contentType;

    const req = https.request(opts, (res) => {
      let data = '';
      res.on('data', d => data += d);
      res.on('end', () => {
        try { resolve({ status: res.statusCode, data: JSON.parse(data) }); }
        catch (e) { resolve({ status: res.statusCode, data: data }); }
      });
    });
    req.on('error', reject);
    req.setTimeout(30000, () => { req.destroy(); reject(new Error('timeout')); });
    if (body) req.write(body);
    req.end();
  });
}

function multipartUpload(apiPath, token, files, manifest) {
  return new Promise((resolve, reject) => {
    const boundary = '----CFPagesDeploy' + Date.now();
    const parts = [];

    // Add manifest
    parts.push(
      `--${boundary}\r\n` +
      `Content-Disposition: form-data; name="manifest"\r\n\r\n` +
      `${JSON.stringify(manifest)}\r\n`
    );

    // Add each file
    for (const f of files) {
      const content = fs.readFileSync(path.join(DEPLOY_DIR, f.localPath));
      parts.push(
        `--${boundary}\r\n` +
        `Content-Disposition: form-data; name="${f.hash}"; filename="${f.urlPath}"\r\n` +
        `Content-Type: application/octet-stream\r\n\r\n`
      );
      parts.push(content);
      parts.push('\r\n');
    }

    parts.push(`--${boundary}--\r\n`);

    // Convert to single buffer
    const buffers = parts.map(p => typeof p === 'string' ? Buffer.from(p) : p);
    const body = Buffer.concat(buffers);

    const opts = {
      hostname: 'api.cloudflare.com',
      path: apiPath,
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + token,
        'Content-Type': `multipart/form-data; boundary=${boundary}`,
        'Content-Length': body.length,
      },
    };

    const req = https.request(opts, (res) => {
      let data = '';
      res.on('data', d => data += d);
      res.on('end', () => {
        try { resolve({ status: res.statusCode, data: JSON.parse(data) }); }
        catch (e) { resolve({ status: res.statusCode, data: data }); }
      });
    });
    req.on('error', reject);
    req.setTimeout(60000, () => { req.destroy(); reject(new Error('upload timeout')); });
    req.write(body);
    req.end();
  });
}

async function main() {
  console.log('╔══════════════════════════════════════╗');
  console.log('║  Cloudflare Pages — Direct Upload    ║');
  console.log('╚══════════════════════════════════════╝');
  console.log();

  const token = getToken();
  console.log(`[1/3] Auth token loaded (${token.length} chars)`);

  // Build manifest: { "/path": hash }
  console.log('[2/3] Building manifest...');
  const manifest = {};
  const fileData = [];

  for (const f of FILES) {
    const fullPath = path.join(DEPLOY_DIR, f);
    const content = fs.readFileSync(fullPath);
    const hash = sha256hex(content);
    const urlPath = '/' + f;
    manifest[urlPath] = hash;
    fileData.push({ localPath: f, urlPath, hash, size: content.length });
    console.log(`  ${urlPath.padEnd(30)} ${hash.slice(0, 12)}… (${content.length} bytes)`);
  }

  console.log(`  ${FILES.length} files, manifest ready`);

  // Upload
  console.log('[3/3] Uploading to Cloudflare Pages...');
  const apiPath = `/client/v4/accounts/${ACCOUNT_ID}/pages/projects/${PROJECT_NAME}/deployments`;

  const result = await multipartUpload(apiPath, token, fileData, manifest);

  if (result.data?.success) {
    const dep = result.data.result;
    console.log();
    console.log('✓ Deploy successful!');
    console.log(`  ID:     ${dep.id}`);
    console.log(`  URL:    ${dep.url}`);
    console.log(`  Env:    ${dep.environment}`);
    if (dep.aliases?.length) console.log(`  Alias:  ${dep.aliases.join(', ')}`);
  } else {
    console.log();
    console.log('✗ Deploy failed!');
    console.log('  Status:', result.status);
    const errors = result.data?.errors || [];
    for (const e of errors) {
      console.log(`  Error ${e.code}: ${e.message}`);
    }
    if (!errors.length) console.log('  Raw:', JSON.stringify(result.data).slice(0, 300));
    process.exit(1);
  }
}

main().catch(e => { console.error('Fatal:', e.message); process.exit(1); });
