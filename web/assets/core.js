export const state = {
  user: null, csrf: null, isAdmin: false, business: null, currentPlan: 'FREE', plan: null,
  subscription: null, features: [], previousPlan: null, publicPlans: null, publicSettings: null,
  cart: [], viewCache: {}, notifyOpen: false, searchTimer: null, pathname: location.pathname,
};

export class ApiError extends Error {
  constructor(message, status, code = '') { super(message); this.status = status; this.code = code; }
}

export async function api(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = new Headers(options.headers || {});
  headers.set('Accept', 'application/json');
  const isForm = options.body instanceof FormData;
  let body = options.body;
  if (body && !isForm && typeof body !== 'string') {
    headers.set('Content-Type', 'application/json');
    body = JSON.stringify(body);
  }
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && state.csrf) headers.set('X-CSRF-Token', state.csrf);
  const response = await fetch(path, { method, headers, body, credentials: 'same-origin', cache: 'no-store' });
  if (options.raw) return response;
  let data = {};
  const type = response.headers.get('content-type') || '';
  if (type.includes('application/json')) {
    try { data = await response.json(); } catch { data = {}; }
  } else if (!response.ok) {
    data = { detail: 'The request could not be completed.' };
  }
  if (!response.ok) {
    throw new ApiError(data.detail || 'Something went wrong. Please try again.', response.status,
      response.headers.get('X-BizFlow-Error') || '');
  }
  return data;
}

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[ch]));
}

const icons = {
  grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  chart:'<path d="M3 3v18h18"/><path d="m7 14 4-4 4 3 6-8"/>',
  bag:'<path d="M5 8h14l1 13H4L5 8Z"/><path d="M9 8a3 3 0 0 1 6 0"/>',
  box:'<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="M3 8v9l9 5 9-5V8"/><path d="M12 13v9"/>',
  layers:'<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 16 9 5 9-5"/>',
  users:'<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
  user:'<circle cx="12" cy="8" r="4"/><path d="M5 21a7 7 0 0 1 14 0"/>',
  truck:'<path d="M3 6h12v11H3z"/><path d="M15 10h4l3 3v4h-7"/><circle cx="7.5" cy="18.5" r="1.5"/><circle cx="18.5" cy="18.5" r="1.5"/>',
  file:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M8 13h8M8 17h8"/>',
  quote:'<path d="M3 21c3 0 5-2 5-5V5H2v7h4c0 2-1 3-3 3v6Zm13 0c3 0 5-2 5-5V5h-6v7h4c0 2-1 3-3 3v6Z"/>',
  wallet:'<path d="M4 6h16a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h14"/><path d="M22 12h-5a2 2 0 0 0 0 4h5"/><path d="M17 14h.01"/>',
  receipt:'<path d="M4 3v18l4-2 4 2 4-2 4 2V3l-4 2-4-2-4 2-4-2Z"/><path d="M8 9h8M8 13h8"/>',
  settings:'<circle cx="12" cy="12" r="3"/><path d="m19.4 15 .1.1 1.4 1.1-1.4 2.4-1.7-.7a8 8 0 0 1-1.7 1l-.3 1.8h-2.8l-.3-1.8a8 8 0 0 1-1.7-1l-1.7.7-1.4-2.4 1.4-1.1a7 7 0 0 1 0-2l-1.4-1.1 1.4-2.4 1.7.7a8 8 0 0 1 1.7-1l.3-1.8h2.8l.3 1.8a8 8 0 0 1 1.7 1l1.7-.7 1.4 2.4-1.4 1.1a7 7 0 0 1 0 2Z" transform="translate(-1 -1)"/>',
  bell:'<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/>',
  search:'<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  arrow:'<path d="M5 12h14M13 6l6 6-6 6"/>',
  back:'<path d="m15 18-6-6 6-6"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  shield:'<path d="M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11Z"/><path d="m9 12 2 2 4-4"/>',
  spark:'<path d="m12 3 1.9 5.8L20 11l-6.1 2.2L12 19l-2-5.8L4 11l6-2.2L12 3Z"/><path d="m19 14 1.1 2.9L23 18l-2.9 1.1L19 22l-1.1-2.9L15 18l2.9-1.1L19 14Z"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  alert:'<path d="M10.3 3.9 2.4 17.5A2 2 0 0 0 4.1 20h15.8a2 2 0 0 0 1.7-2.5L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>',
  eye:'<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  edit:'<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4Z"/>',
  trash:'<path d="M3 6h18M8 6V4h8v2m3 0-1 15H6L5 6m4 4v7m6-7v7"/>',
  download:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
  print:'<path d="M6 9V3h12v6M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><path d="M6 14h12v7H6z"/>',
  whatsapp:'<path d="M20.5 11.5a8.5 8.5 0 0 1-12.6 7.4L3 20l1.1-4.7a8.5 8.5 0 1 1 16.4-3.8Z"/><path d="M8 8.5c.8 3 2.5 4.7 5.5 5.5l1.2-1.2 2 1c-.3 1.3-1.1 2-2.4 2.2-3.7-.5-7.1-3.8-7.7-7.5.2-1.3.9-2.1 2.2-2.4l1 2-1.8 1.4Z"/>',
  menu:'<path d="M4 7h16M4 12h16M4 17h16"/>',
  collapse:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16m4-11 2 3-2 3"/>',
  logout:'<path d="M10 17l5-5-5-5M15 12H3"/><path d="M12 3h7a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-7"/>',
  lock:'<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
  building:'<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 21V9h6v12M7 7h.01M17 7h.01M7 13h.01M17 13h.01"/>',
  calendar:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/>',
  close:'<path d="m18 6-12 12M6 6l12 12"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
};
export function icon(name, cls = '') { return `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.spark}</svg>`; }
export function money(value, currency = state.business?.currency || 'LKR') {
  const n = Number(value || 0);
  try { return new Intl.NumberFormat('en-LK', { style: 'currency', currency, minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n); }
  catch { return `${currency} ${n.toLocaleString('en-LK', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`; }
}
export function dateFmt(value, withTime = false) {
  if (!value) return '—';
  const d = new Date(value.length === 10 ? `${value}T00:00:00+05:30` : value);
  if (Number.isNaN(d.getTime())) return esc(value);
  return new Intl.DateTimeFormat('en-LK', withTime
    ? { day:'2-digit', month:'short', year:'numeric', hour:'numeric', minute:'2-digit', timeZone:'Asia/Colombo' }
    : { day:'2-digit', month:'short', year:'numeric', timeZone:'Asia/Colombo' }).format(d);
}
export function initials(name = '') { return String(name).trim().split(/\s+/).slice(0, 2).map(x => x[0] || '').join('').toUpperCase() || 'B'; }
export function badge(status) {
  const value = String(status || 'FREE').toUpperCase();
  const cls = value.toLowerCase().replaceAll('_', '-').replaceAll(' ', '-');
  return `<span class="badge badge-${esc(cls)}">${esc(value.replaceAll('_', ' '))}</span>`;
}
export function link(path, label, cls = '') { return `<a href="${esc(path)}" data-link class="${cls}">${label}</a>`; }
export function toast(message, kind = 'success') {
  const root = document.getElementById('toast-root');
  const el = document.createElement('div');
  el.className = `toast ${kind === 'error' ? 'toast-error' : kind === 'warning' ? 'toast-warning' : ''}`;
  el.innerHTML = `${icon(kind === 'error' ? 'alert' : kind === 'warning' ? 'clock' : 'check')}<span class="toast-message">${esc(message)}</span><button class="toast-close" aria-label="Dismiss">×</button>`;
  root.appendChild(el);
  const timer = setTimeout(() => el.remove(), 4600);
  el.querySelector('.toast-close').onclick = () => { clearTimeout(timer); el.remove(); };
}
export function modal(title, subtitle, body, footer = '') {
  const root = document.getElementById('modal-root');
  if (!root) return;
  root.innerHTML = `<div class="modal-backdrop" data-action="backdrop"><section class="modal ${body.length > 2500 ? 'modal-wide' : ''}" role="dialog" aria-modal="true" aria-label="${esc(title)}"><header class="modal-head"><div><h2>${esc(title)}</h2>${subtitle ? `<p>${esc(subtitle)}</p>` : ''}</div><button class="modal-close" data-action="close-modal" aria-label="Close">×</button></header><div class="modal-body">${body}</div>${footer ? `<footer class="modal-footer">${footer}</footer>` : ''}</section></div>`;
  root.querySelector('.modal-backdrop')?.addEventListener('click', e => { if (e.target.classList.contains('modal-backdrop')) closeModal(); });
  root.querySelector('input:not([type=hidden]),select,textarea')?.focus({preventScroll:true});
}
export function closeModal() { const root = document.getElementById('modal-root'); if (root) root.innerHTML = ''; }
export function navigate(path, { replace = false } = {}) {
  if (replace) history.replaceState({}, '', path); else history.pushState({}, '', path);
  state.pathname = location.pathname;
  window.dispatchEvent(new PopStateEvent('popstate'));
  const reduced=window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  window.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' });
}
export function emptyState(title, copy, actionLabel = '', action = '') {
  return `<div class="empty-state"><div class="empty-art">${icon('box')}</div><h3>${esc(title)}</h3><p>${esc(copy)}</p>${actionLabel ? `<button class="btn btn-primary btn-small" data-action="${esc(action)}">${icon('plus')}${esc(actionLabel)}</button>` : ''}</div>`;
}
export function errorPanel(error, path = '/pricing') {
  const planLock = error?.status === 402;
  return `<div class="page-lock"><div class="lock-icon">${icon(planLock ? 'lock' : 'alert')}</div><h2>${planLock ? 'This feature needs an upgrade' : 'We couldn’t load this page'}</h2><p class="section-intro" style="margin:8px auto 18px">${esc(error?.message || 'Please try again in a moment.')}</p><div class="heading-actions" style="justify-content:center"><a class="btn btn-primary" data-link href="${planLock ? '/pricing' : '/dashboard'}">${planLock ? 'View plans' : 'Back to dashboard'}</a><button class="btn btn-ghost" data-action="retry-page">Try again</button></div></div>`;
}
export const planBullets = {
  FREE: ['Basic business dashboard', 'Up to 50 products', '50 invoices per month', 'Basic sales & inventory', '1 team member'],
  STARTER: ['Unlimited products', 'Unlimited invoices', 'Inventory management', 'Customers & suppliers', 'Quotations & expense tracking', 'Sales reports & PDF invoices', 'WhatsApp sharing · 2 staff accounts'],
  BUSINESS: ['Everything in Starter', 'Unlimited staff', 'Advanced reports & profit overview', 'Low stock & sales analytics', 'Expense & customer analytics', 'Multiple business locations', 'Data export & priority support'],
};
export function planCard(plan, compact = false) {
  const id = plan.id;
  const price = Number(plan.price || 0);
  const starter = id === 'STARTER';
  const planName = id === 'FREE' ? 'Free' : id[0] + id.slice(1).toLowerCase();
  const bullets = planBullets[id] || (plan.features || []).map(f => f.replaceAll('_', ' '));
  return `<article class="price-card ${starter ? 'featured' : ''} ${compact ? 'plan-card-compact' : ''}">${starter ? '<span class="plan-ribbon">Most popular</span>' : ''}<div class="plan-name">${esc(planName)}</div><div class="plan-price"><strong>${price === 0 ? 'LKR 0' : money(price, 'LKR')}</strong>${price ? `<span>for ${Number(plan.duration_days || 30)} days</span>` : ''}</div><div class="plan-period">${price ? 'Starts after administrator approval' : 'Basic business management'}</div><p class="plan-desc">${id === 'FREE' ? 'Start managing your business with no payment and no time limit.' : id === 'STARTER' ? 'Simple, capable tools for a growing small business.' : 'More visibility and room to scale with your team.'}</p><ul class="plan-features">${bullets.map(f => `<li>${icon('check')}${esc(f)}</li>`).join('')}</ul><button class="btn ${starter ? 'btn-primary' : 'btn-soft'} plan-card-action" data-action="choose-plan" data-plan="${esc(id)}">${id === 'FREE' ? 'Start for free' : `Choose ${esc(planName)}`}</button></article>`;
}
