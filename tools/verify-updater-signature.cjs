#!/usr/bin/env node
// Verify Tauri's base64 Minisign envelope with Node's built-in Ed25519 verifier.
'use strict';
const fs = require('node:fs');
const crypto = require('node:crypto');

function decode(value) {
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(value) || value.length % 4 !== 0) throw new Error('Invalid base64');
  return Buffer.from(value, 'base64');
}
async function main() {
  const [installerPath, signaturePath, configPath] = process.argv.slice(2);
  if (!installerPath || !signaturePath || !configPath) throw new Error('Expected installer, signature and Tauri config paths');
  const config = JSON.parse(fs.readFileSync(configPath, 'utf8').replace(/^\uFEFF/, ''));
  const keyLines = decode(config.plugins.updater.pubkey.trim()).toString('utf8').trim().split(/\r?\n/);
  const key = decode(keyLines.at(-1));
  if (key.length !== 42 || !['Ed', 'ED'].includes(key.subarray(0, 2).toString())) throw new Error('Invalid public key');
  const envelope = fs.readFileSync(signaturePath, 'utf8').trim();
  const lines = decode(envelope).toString('utf8').trim().split(/\r?\n/);
  if (lines.length !== 4 || !lines[0].startsWith('untrusted comment: ') || !lines[2].startsWith('trusted comment: ')) throw new Error('Invalid Minisign envelope');
  const record = decode(lines[1]), global = decode(lines[3]);
  if (record.length !== 74 || global.length !== 64 || !record.subarray(2, 10).equals(key.subarray(2, 10))) throw new Error('Signature key mismatch');
  const algorithm = record.subarray(0, 2).toString();
  if (!['ED', 'Ed'].includes(algorithm)) throw new Error('Unsupported signature algorithm');
  const publicKey = crypto.createPublicKey({key: Buffer.concat([Buffer.from('302a300506032b6570032100', 'hex'), key.subarray(10)]), format: 'der', type: 'spki'});
  let message;
  if (algorithm === 'ED') {
    const hash = crypto.createHash('blake2b512');
    for await (const chunk of fs.createReadStream(installerPath)) hash.update(chunk);
    message = hash.digest();
  } else message = fs.readFileSync(installerPath);
  const signature = record.subarray(10);
  if (!crypto.verify(null, message, publicKey, signature) || !crypto.verify(null, Buffer.concat([signature, Buffer.from(lines[2].slice(17))]), publicKey, global)) throw new Error('Updater signature verification failed');
  console.log(JSON.stringify({verified: true, key_id: key.subarray(2, 10).toString('hex')}));
}
main().catch((error) => { console.error(error.message); process.exitCode = 1; });
