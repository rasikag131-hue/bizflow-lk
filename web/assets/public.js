import { state, api, esc, icon, link, planCard, planBullets, money } from './core.js';

async function ensurePublicData(path) {
  if ((path === '/' || path === '/pricing') && state.publicPlans === null) {
    try {
      const response = await api('/api/billing/plans');
      state.publicPlans = Array.isArray(response.plans) ? response.plans : [];
      state.publicPlansUnavailable = state.publicPlans.length === 0;
    } catch {
      state.publicPlans = [];
      state.publicPlansUnavailable = true;
    }
  }
  if (path === '/contact' && state.publicSettings === null) {
    try { state.publicSettings = await api('/api/public/settings'); }
    catch { state.publicSettings = {}; }
  }
}

function plansUnavailable() {
  return `<div class="notice notice-warning" role="status">Current plan details could not be loaded. Please try again shortly.</div>`;
}

function whatsappDigits(value) {
  const digits=String(value||'').replace(/\D/g,'');
  return digits.startsWith('0')?`94${digits.slice(1)}`:digits;
}
function header() {
  return `<header class="public-header"><a href="/" data-link class="brand" aria-label="Bizflow.lk home"><img class="brand-logo" src="/assets/bizflow-logo.png" alt="Bizflow.lk"></a><nav class="public-nav" id="public-nav"><a href="/features" data-link>Features</a><a href="/#how-it-works" data-link>Solutions</a><a href="/pricing" data-link>Pricing</a><a href="/faq" data-link>FAQ</a></nav><div class="public-actions">${state.user ? `<a class="btn btn-ghost btn-small" href="/account" data-link>My account</a><a class="btn btn-primary btn-small" href="${state.isAdmin ? '/admin' : '/dashboard'}" data-link>Open dashboard ${icon('arrow')}</a>` : `<a class="btn btn-ghost btn-small" href="/login" data-link>Log in</a><a class="btn btn-primary btn-small" href="/register" data-link>Start free ${icon('arrow')}</a>`}<button class="icon-btn public-mobile-toggle" data-action="public-menu" aria-label="Open menu" aria-expanded="false">${icon('menu')}</button></div></header>`;
}
function footer() {
  const year = new Date().getFullYear();
  return `<footer class="public-footer"><div class="footer-inner"><div><a href="/" data-link class="brand" aria-label="Bizflow.lk home"><img class="brand-logo" src="/assets/bizflow-logo.png" alt="Bizflow.lk"></a><div class="footer-copy" style="margin-top:12px">Simple tools for the work behind your business.</div></div><div class="footer-links"><a href="/privacy" data-link>Privacy</a><a href="/terms" data-link>Terms</a><a href="/faq" data-link>FAQ</a><a href="/contact" data-link>Contact</a></div><div class="footer-copy">© ${year} BizFlow LK</div></div></footer>`;
}
function publicFrame(content) {
  return `<div class="public-shell">${header()}${content}${footer()}<div id="modal-root"></div></div>`;
}

const featureCards = [
  ['bag','Sales without the spreadsheets','A fast checkout flow for your counter. Add products, choose a customer, record a payment and keep stock in sync.'],
  ['box','Inventory you can trust','Know what is in stock, what is running low and what moved. Every stock adjustment has a traceable movement.'],
  ['receipt','Invoices that look professional','Create, print and share invoices with paid, partial and outstanding balances kept in one place.'],
  ['users','Customers & suppliers','Keep useful contacts, link suppliers to products and review recorded stock purchases alongside customer activity.'],
  ['chart','Reports from real activity','Review your sales, expenses and inventory using figures from your own business records.'],
  ['shield','Your data stays yours','A Free plan is ready when you sign up. If paid access expires, your business records stay in place.'],
];
function featureGrid() {
  return `<div class="feature-grid">${featureCards.map(([ico,title,copy]) => `<article class="feature-card"><div class="feature-icon">${icon(ico)}</div><h3>${esc(title)}</h3><p>${esc(copy)}</p></article>`).join('')}</div>`;
}
function heroPreview() {
  return `<div class="hero-visual" data-reveal="fade"><div class="preview-window" aria-label="Illustrative BizFlow dashboard preview"><div class="preview-top"><span class="preview-top-brand"><i class="preview-dot"></i> BIZFLOW</span><span class="preview-top-label">WORKSPACE PREVIEW</span><span class="preview-preview-tag">ILLUSTRATIVE</span></div><div class="preview-content"><div class="preview-rail" aria-hidden="true"><span class="active"></span><span></span><span></span><span></span><span></span></div><div class="preview-board"><div class="preview-board-heading"><div><span class="preview-overline">WORKSPACE OVERVIEW</span><h3>Your business, at a glance</h3><p>Sales, stock and customer balances together.</p></div><span class="preview-current-plan">FREE PLAN</span></div><div class="preview-metrics"><div class="preview-metric"><small>Today's sales</small><strong>LKR —</strong></div><div class="preview-metric"><small>Outstanding</small><strong>LKR —</strong></div><div class="preview-metric"><small>Low stock</small><strong>— items</strong></div></div><div class="preview-chart-wrap"><div class="preview-chart-title"><strong>Sales activity</strong><span>Illustrative layout</span></div><div class="preview-chart" aria-hidden="true"><i style="--h:30%"></i><i style="--h:45%"></i><i style="--h:38%"></i><i style="--h:63%"></i><i style="--h:52%"></i><i style="--h:72%"></i><i style="--h:58%"></i><i style="--h:82%"></i><i style="--h:68%"></i><i style="--h:92%"></i><i style="--h:75%"></i><i style="--h:100%"></i></div></div><div class="preview-bottom"><div class="preview-line"><span class="preview-line-icon">${icon('receipt')}</span><span><small>Recent activity</small><b>Ready for your first sale</b></span></div><div class="preview-line"><span class="preview-line-icon">${icon('box')}</span><span><small>Inventory</small><b>Add a product to begin</b></span></div><div class="preview-line"><span class="preview-line-icon">${icon('shield')}</span><span><small>Plan access</small><b>Free · no expiry</b></span></div></div></div></div><div class="hero-stamp">Your data appears after you sign up</div></div>`;
}
function homePage(plans) {
  const display = plans.filter(p => ['FREE','STARTER','BUSINESS'].includes(p.id)).sort((a,b) => ['FREE','STARTER','BUSINESS'].indexOf(a.id)-['FREE','STARTER','BUSINESS'].indexOf(b.id));
  return `<main><section class="hero"><div class="hero-inner"><div><div class="eyebrow eyebrow-light"><span class="eyebrow-dot"></span>MADE FOR THE WAY SMALL BUSINESSES WORK</div><h1>Run Your Business.<br><span>From One Simple Dashboard.</span></h1><p class="hero-copy">Keep sales, products, stock, customers and invoices in one calm, clear workspace—so you can spend less time chasing details and more time running your business.</p><div class="hero-actions"><a href="/register" data-link class="btn btn-primary">Start free ${icon('arrow')}</a><a href="/#how-it-works" data-link class="btn btn-ghost">See how it works</a></div><div class="hero-note">${icon('shield')}Free to start. Paid access begins only after an administrator verifies your bank transfer.</div></div>${heroPreview()}</div></section><div class="trust-strip"><div class="trust-inner"><span class="trust-item">${icon('check')}Start on Free</span><span class="trust-item">${icon('wallet')}LKR pricing</span><span class="trust-item">${icon('clock')}Paid access for 30 days</span><span class="trust-item">${icon('shield')}Manual bank verification</span></div></div><section class="section"><div class="section-inner"><div class="section-kicker">THE EVERYDAY ESSENTIALS</div><h2 class="section-heading">The moving parts of your business,<br>working together.</h2><p class="section-intro">From your first product to your next invoice, BizFlow helps keep the important details clear and connected.</p>${featureGrid()}</div></section><section class="section how-section" id="how-it-works"><div class="section-inner"><div class="center"><div class="section-kicker">SIMPLE BY DESIGN</div><h2 class="section-heading">Up and running in a few steps.</h2><p class="section-intro">Start free. Take your time. Upgrade only when your business is ready.</p></div><div class="steps-grid"><div class="step"><span class="step-number">01</span><div><h3>Create your account</h3><p>Register with your details. Your Free plan is ready right away.</p></div></div><div class="step"><span class="step-number">02</span><div><h3>Add your business details</h3><p>Set up your workspace, add a product and start recording real business activity.</p></div></div><div class="step"><span class="step-number">03</span><div><h3>Upgrade when you need more</h3><p>Transfer the plan amount to the displayed bank account and send your receipt. Paid access starts only after administrator approval.</p></div></div></div></div></section><section class="section"><div class="section-inner"><div class="center"><div class="section-kicker">CLEAR PLANS. NO SURPRISES.</div><h2 class="section-heading">Choose a plan that fits today.</h2><p class="section-intro">Free does not expire. Paid plans give you 30 days of access after an administrator approves your bank transfer.</p></div>${state.publicPlansUnavailable ? plansUnavailable() : `<div class="pricing-grid">${display.map(p => planCard(p, true)).join('')}</div>`}<p class="billing-note">${icon('info')}No online gateway or automatic recurring billing. Every paid transfer is checked by the BizFlow administrator.</p></div></section><section class="section section-narrow"><div class="section-inner"><div class="cta-band"><div><h2>Make the next business day feel simpler.</h2><p>Start with Free and build your workspace as you go.</p></div><a href="/register" data-link class="btn btn-white">Create a free account ${icon('arrow')}</a></div></div></section><section class="section section-narrow"><div class="section-inner"><div class="center"><div class="section-kicker">GOOD TO KNOW</div><h2 class="section-heading">Questions, answered.</h2><p class="section-intro">A few details about plans, payments and your business data.</p></div>${faqList(true)}</div></section></main>`;
}
function faqList(short = false) {
  const items = [
    ['Can I use BizFlow without paying?','Yes. Every new account starts on the Free plan. You can continue using Free without a time limit, within its product, invoice and team limits.'],
    ['How do paid plans work?','Choose Starter or Business, transfer the displayed amount to the bank account shown in Billing, and upload your receipt. The request remains Pending until a BizFlow administrator personally checks the transfer and approves it.'],
    ['Does uploading a receipt activate my plan?','No. Uploading proof only submits a payment request. It does not verify the bank transfer and does not activate paid features. Only an administrator can approve it.'],
    ['How long does an approved plan last?','Each approved paid subscription lasts 30 days by default. Early renewals add 30 days to your current expiry. After expiry, the account returns to Free and its business data is kept.'],
    ['Is the plan billed automatically each month?','No. Version 1 does not use an online payment gateway or automatic recurring billing. Renew by bank transfer and wait for administrator approval.'],
    ['What happens to my records if my paid plan expires?','Your products, customers, sales, invoices and other business records remain stored. Paid-only features become unavailable until you renew; your Free plan remains available.'],
  ];
  return `<div class="faq-list">${items.slice(0, short ? 4 : items.length).map(([q,a]) => `<details class="faq-item"><summary>${esc(q)}</summary><p>${esc(a)}</p></details>`).join('')}${short ? `<div class="center" style="margin-top:6px"><a class="btn btn-ghost btn-small" data-link href="/faq">Read all FAQs ${icon('arrow')}</a></div>` : ''}</div>`;
}

export async function renderPublic(path) {
  await ensurePublicData(path);
  if (path === '/pricing') {
    const plans = state.publicPlans.filter(p => ['FREE','STARTER','BUSINESS'].includes(p.id)).sort((a,b) => ['FREE','STARTER','BUSINESS'].indexOf(a.id)-['FREE','STARTER','BUSINESS'].indexOf(b.id));
    return publicFrame(`<main class="public-content"><div class="section-kicker">STRAIGHTFORWARD PRICING</div><h1>Start free. Upgrade when it makes sense.</h1><p>Choose what works for your business now. Paid access lasts 30 days from administrator approval—there is no automatic recurring charge.</p>${state.publicPlansUnavailable ? plansUnavailable() : `<div class="pricing-grid" style="margin-top:34px">${plans.map(p => planCard(p)).join('')}</div>`}<div class="notice notice-warning" style="margin-top:22px">${icon('info')}<span><strong>Manual bank transfer only.</strong> After selecting a paid plan, transfer the amount shown, upload your receipt and submit the request. Your plan stays Free until a BizFlow administrator verifies and approves it.</span></div><div style="margin-top:25px">${faqList(true)}</div></main>`);
  }
  if (path === '/features') {
    return publicFrame(`<main class="public-content"><div class="section-kicker">ONE WORKSPACE, LESS BUSYWORK</div><h1>Tools for the work behind every sale.</h1><p>BizFlow LK brings the business basics into one workspace. You start on Free and choose paid capabilities only if and when you need them.</p>${featureGrid()}<section class="section" style="padding-left:0;padding-right:0"><div class="cta-band"><div><h2>Give your business a clearer view.</h2><p>Set up your Free workspace in minutes.</p></div><a class="btn btn-white" data-link href="/register">Start free ${icon('arrow')}</a></div></section></main>`);
  }
  if (path === '/faq') return publicFrame(`<main class="public-content"><div class="section-kicker">HELP & CLARITY</div><h1>Frequently asked questions.</h1><p>Understand how BizFlow plans, manual payment verification and access periods work.</p>${faqList(false)}</main>`);
  if (path === '/contact') {
    const s = state.publicSettings || {};
    const phone = String(s.support_phone || '').trim();
    const whatsapp = whatsappDigits(s.support_whatsapp || s.whatsapp_number || '');
    const contactItems = [s.support_email ? `<div class="contact-card"><div class="feature-icon">${icon('file')}</div><h3>Email support</h3><p>${esc(s.support_email)}</p><a class="btn btn-soft btn-small" href="mailto:${esc(s.support_email)}">Write an email</a></div>` : '', phone ? `<div class="contact-card"><div class="feature-icon">${icon('user')}</div><h3>Phone support</h3><p>${esc(phone)}</p><a class="btn btn-soft btn-small" href="tel:${esc(phone.replace(/[^+0-9]/g,''))}">Call support</a></div>` : '', whatsapp ? `<div class="contact-card"><div class="feature-icon">${icon('whatsapp')}</div><h3>WhatsApp</h3><p>${esc(s.support_message || 'Message the BizFlow support team.')}</p><a class="btn btn-soft btn-small" target="_blank" rel="noopener noreferrer" href="https://wa.me/${esc(whatsapp)}">Open WhatsApp</a></div>` : ''].filter(Boolean).join('');
    return publicFrame(`<main class="public-content"><div class="section-kicker">WE’RE HERE TO HELP</div><h1>Contact BizFlow LK.</h1><p>For help with an account, payment request or plan, use one of the configured support channels below. Receipt uploads are private and are only visible to the uploader and authorized administrators.</p>${contactItems ? `<div class="contact-grid">${contactItems}</div>` : '<div class="notice notice-info" role="status">Support contact details have not been configured yet.</div>'}</main>`);
  }
  if (path === '/privacy' || path === '/terms') {
    const privacy = path === '/privacy';
    return publicFrame(`<main class="public-content legal-copy"><div class="section-kicker">BIZFLOW LK</div><h1>${privacy ? 'Privacy notice' : 'Terms of use'}</h1><p>Last updated: 4 October 2026</p>${privacy ? `<h2>Information we use</h2><p>BizFlow stores the account, business and activity information that you provide so the service can operate. Passwords are securely hashed. Identity numbers are validated, encrypted at rest, and shown to customers only in masked form. Full identity-number access is restricted to authorized Super Administrators and is recorded in the audit history.</p><h2>Business data and access</h2><p>Your business records are scoped to your authorized business membership. Platform administrators can access payment requests and receipts for manual verification and may access account information to provide administration or support. Identity numbers are not included in support messages. Uploaded receipts are stored privately and served only to the uploader or an authorized administrator.</p><h2>Subscriptions</h2><p>New accounts start on Free. A payment receipt does not automatically activate a paid plan. Paid access begins only after administrator approval and lasts for the stated period; default access is 30 days. Expiry changes access, not ownership or retention of historical business records.</p><h2>Security and contact</h2><p>We use access controls, protected sessions and private storage to help safeguard account information. No internet service can promise absolute security. Contact the support channel shown on the Contact page with privacy questions.</p>` : `<h2>Account and acceptable use</h2><p>Provide accurate account information, keep your sign-in details safe and use BizFlow only for lawful business activity. You are responsible for activity carried out under your account.</p><h2>Free and paid plans</h2><p>Every new customer account starts on Free. Paid plans are paid manually by bank transfer and require administrator verification. Uploading a receipt creates a Pending request only; it does not constitute approval. An approved paid subscription lasts 30 days by default and may be extended or changed by an administrator.</p><h2>Expiry and records</h2><p>After expiry, paid-only features are disabled and the effective plan returns to Free. Expiry does not itself delete business records. Usage limits and plan features are enforced server-side.</p><h2>Availability and changes</h2><p>BizFlow is provided as a business management service and availability may change as the product is maintained. Accounts may be suspended for security, payment or acceptable-use concerns. Important administrative subscription actions are recorded for accountability.</p>`}</main>`);
  }
  return publicFrame(homePage(state.publicPlans));
}

function authBrand() {
  return `<a class="brand auth-brand" href="/" data-link aria-label="Bizflow.lk home"><span class="brand-logo-panel"><img class="brand-logo" src="/assets/bizflow-logo.png" alt="Bizflow.lk"></span></a>`;
}
function authShell(form, heading, copy, mode) {
  const title = mode === 'register' ? 'Start with a free account' : mode === 'forgot' ? 'Get help signing in' : 'Welcome back';
  const strap = mode === 'register' ? 'YOUR BUSINESS, IN BETTER FLOW' : mode === 'forgot' ? 'ACCOUNT SUPPORT' : 'PICK UP WHERE YOU LEFT OFF';
  const sub = mode === 'register' ? 'No payment details required. Your account starts on the Free plan.' : mode === 'login' ? 'Sign in with your username or email and password.' : 'Forgot your password? Contact BizFlow support and an administrator can issue a one-time temporary password.';
  return `<main class="auth-page"><section class="auth-left">${authBrand()}<div class="auth-copy"><div class="eyebrow eyebrow-light"><span class="eyebrow-dot"></span>${strap}</div><h1>${heading}</h1><p>${copy}</p><div class="auth-points"><div>${icon('check')}Free plan, ready as soon as you sign up</div><div>${icon('shield')}Private business data and clear access controls</div><div>${icon('clock')}Paid plans last 30 days after approval</div></div></div><div class="auth-legal">© ${new Date().getFullYear()} BizFlow LK · Made for small business</div></section><section class="auth-right"><div class="auth-card"><a class="auth-back" href="/" data-link>${icon('back')}Back to BizFlow</a><h2>${title}</h2><p class="auth-sub">${sub}</p>${form}</div></section></main>`;
}

export function renderAuth(path) {
  if (path === '/register') {
    const form = `<div class="auth-form-wrap"><form class="form-stack" data-form="register">
      <div class="form-grid">
        <div class="field"><label for="reg-name">Full name</label><input id="reg-name" name="full_name" autocomplete="name" required maxlength="120" placeholder="Your name"></div>
        <div class="field"><label for="reg-identity">Identity number</label><input id="reg-identity" name="identity_number" autocomplete="off" required maxlength="80" placeholder="Your national identity number"><span class="field-hint">Encrypted and shown to you only as a masked value.</span></div>
      </div>
      <div class="form-grid">
        <div class="field"><label for="reg-email">Email address</label><input id="reg-email" name="email" type="email" autocomplete="email" required maxlength="254" placeholder="you@business.com"></div>
        <div class="field"><label for="reg-phone">Phone</label><input id="reg-phone" name="phone" type="tel" autocomplete="tel" required maxlength="40" placeholder="077 123 4567"></div>
      </div>
      <div class="form-grid">
        <div class="field"><label for="reg-business">Business name</label><input id="reg-business" name="business_name" required maxlength="160" placeholder="Your business"></div>
        <div class="field"><label for="reg-type">Business type</label><select id="reg-type" name="business_type"><option>Retail</option><option>Food & beverage</option><option>Services</option><option>Wholesale</option><option>Beauty & wellness</option><option>Other</option></select></div>
      </div>
      <div class="form-grid">
        <div class="field"><label for="reg-password">Password</label><input id="reg-password" name="password" type="password" autocomplete="new-password" minlength="10" maxlength="128" required placeholder="At least 10 characters"><span class="field-hint">Your password is securely hashed.</span></div>
        <div class="field"><label for="reg-confirm-password">Confirm password</label><input id="reg-confirm-password" name="confirm_password" type="password" autocomplete="new-password" minlength="10" maxlength="128" required placeholder="Type it again"></div>
      </div>
      <label class="check-row"><input type="checkbox" name="accepted" required><span>I agree to the <a href="/terms" data-link><strong>Terms</strong></a> and <a href="/privacy" data-link><strong>Privacy notice</strong></a>.</span></label>
      <button class="btn btn-primary" type="submit">Create free account ${icon('arrow')}</button>
    </form><p class="auth-switch">Already have an account? <a href="/login" data-link>Sign in</a></p></div>`;
    return authShell(form, 'Your business starts here.', 'Bring the everyday essentials into one clear, practical workspace. No card. No pressure to upgrade.', 'register');
  }
  if (path === '/forgot-password') {
    const s = state.publicSettings || {};
    const phone = String(s.support_phone || '').trim();
    const whatsapp = whatsappDigits(s.support_whatsapp || s.whatsapp_number || '');
    const phoneHref = phone.replace(/[^+0-9]/g, '');
    const recovery = `<form class="form-stack" data-form="forgot-support">
      <div class="notice notice-info">${icon('info')}<span>${esc(s.support_message || 'Contact the configured BizFlow support channel for help. An administrator can issue a temporary password after verifying your account.')}</span></div>
      <p class="field-hint">Do not enter or send your identity number. It is not needed for this support request.</p>
      <div class="form-grid"><div class="field"><label for="recovery-account">Account ID (if known)</label><input id="recovery-account" name="account_id" autocomplete="off" maxlength="32" placeholder="USR-10001"></div><div class="field"><label for="recovery-username">Username (if known)</label><input id="recovery-username" name="username" autocomplete="username" maxlength="60" placeholder="BIZ-10001"></div></div>
      <div class="field"><label for="recovery-name">Full name</label><input id="recovery-name" name="full_name" autocomplete="name" maxlength="120" required></div>
      <div class="form-grid"><div class="field"><label for="recovery-email">Email address</label><input id="recovery-email" name="email" type="email" autocomplete="email" maxlength="254" required></div><div class="field"><label for="recovery-phone">Phone</label><input id="recovery-phone" name="phone" type="tel" autocomplete="tel" maxlength="40" required></div></div>
      <div class="form-grid">${phone ? `<a class="btn btn-soft" href="tel:${esc(phoneHref)}">${icon('user')}Call ${esc(phone)}</a>` : ''}${whatsapp ? `<button class="btn btn-primary" type="button" data-action="recovery-whatsapp" data-whatsapp="${esc(whatsapp)}">${icon('whatsapp')}Prepare WhatsApp message</button>` : ''}</div>
      ${s.support_email ? `<p class="auth-switch">Support email: <a href="mailto:${esc(s.support_email)}">${esc(s.support_email)}</a></p>` : ''}
      ${!phone && !whatsapp && !s.support_email ? '<div class="notice notice-warning" role="status">Support contact details are not configured yet.</div>' : ''}
      <p class="auth-switch">Remembered your password? <a href="/login" data-link>Back to sign in</a></p>
    </form>`;
    return authShell(recovery, 'Let’s get you back in.', 'Forgot your password? Contact support and an administrator can issue a one-time temporary password after verifying your account.', 'forgot');
  }
  const form = `<div><form class="form-stack" data-form="login">
    <div class="field"><label for="login-id">Username or email</label><input id="login-id" name="identifier" autocomplete="username" required placeholder="BIZ-10001 or you@business.com"></div>
    <div class="field"><label for="login-password">Password</label><input id="login-password" name="password" type="password" autocomplete="current-password" required placeholder="Your password"></div>
    <div class="auth-mini"><span>Secure sign in</span><a href="/forgot-password" data-link>Forgot password?</a></div>
    <button class="btn btn-primary" type="submit">Sign in ${icon('arrow')}</button>
  </form><p class="auth-switch">New to BizFlow? <a href="/register" data-link>Create a free account</a></p></div>`;
  return authShell(form, 'Good to have you back.', 'Your business, activity and next steps are waiting in your workspace.', 'login');
}
