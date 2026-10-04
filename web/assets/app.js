import { state, api, ApiError, esc, icon, navigate, toast, modal, closeModal, money, dateFmt, initials, errorPanel, emptyState } from './core.js';
import { renderPublic, renderAuth } from './public.js';
import { renderPage, handleAction, handleForm, handleInput, handleChange } from './pages.js';
import { renderAdmin, handleAdminAction, handleAdminForm, handleAdminFilter, handleAdminUserFilter } from './admin_views.js';

const appRoot=document.getElementById('app');
let renderVersion=0;
const publicPaths=new Set(['/','/pricing','/features','/faq','/contact','/privacy','/terms','/login','/register','/forgot-password']);
const seoPages={
  '/':['BizFlow LK — one clear view of your business','Run your business from one simple dashboard with sales, inventory, customers and invoices in one workspace.'],
  '/features':['BizFlow LK features — sales, inventory and invoices','Explore the business tools available in BizFlow LK.'],
  '/pricing':['BizFlow LK pricing — Free, Starter and Business','Review current BizFlow LK plans. Paid access starts only after administrator approval of a bank transfer.'],
  '/faq':['BizFlow LK FAQ — plans, payments and account access','Answers about BizFlow LK plans, manual bank transfers, expiry and support.'],
  '/contact':['Contact BizFlow LK support','Contact channels configured by the BizFlow LK administrator.'],
  '/privacy':['BizFlow LK privacy notice','Learn what account and business information BizFlow LK stores and how access is controlled.'],
  '/terms':['BizFlow LK terms of use','Terms for using the BizFlow LK business management service.'],
};
function setMeta(name,content,property=false){
  const selector=property?`meta[property="${name}"]`:`meta[name="${name}"]`;
  let element=document.head.querySelector(selector);
  if(!element){element=document.createElement('meta');if(property)element.setAttribute('property',name);else element.setAttribute('name',name);document.head.appendChild(element);}
  element.setAttribute('content',content);
}
function updateSeo(path){
  const meta=seoPages[path]||['BizFlow LK — private workspace','Sign in to access your BizFlow LK account.'];
  document.title=meta[0];setMeta('description',meta[1]);
  const indexable=Object.hasOwn(seoPages,path);
  setMeta('robots',indexable?'index,follow':'noindex,nofollow');
  setMeta('og:title',meta[0],true);setMeta('og:description',meta[1],true);
  const canonical=new URL(path||'/',location.origin);canonical.search='';canonical.hash='';
  let link=document.head.querySelector('link[rel="canonical"]');
  if(!link){link=document.createElement('link');link.rel='canonical';document.head.appendChild(link);}
  link.href=canonical.href;setMeta('og:url',canonical.href,true);
}

function applySession(data){
  state.user=data.user||null;state.csrf=data.csrf_token||null;state.isAdmin=Boolean(data.is_admin||data.user?.role==='SUPER_ADMIN');
  if(state.user&&data.membership_role)state.user.membership_role=data.membership_role;
  state.business=data.business||null;state.currentPlan=data.current_plan||'FREE';state.plan=data.plan||null;state.subscription=data.subscription||null;
  state.features=data.features||[];state.previousPlan=data.previous_plan||null;
  if(data.account_status&&state.user)state.user.account_status=data.account_status;
  if(data.must_change_password&&state.user)state.user.must_change_password=1;
  if(state.user)state.user.account_status=data.account_status||state.user.account_status||'ACTIVE';
}
function clearSession(){state.user=null;state.csrf=null;state.isAdmin=false;state.business=null;state.currentPlan='FREE';state.plan=null;state.subscription=null;state.features=[];state.previousPlan=null;state.adminUserFilters=null;clearTimeout(state.adminUserTimer);}
async function loadSession(){
  const data=await api('/api/auth/me');applySession(data);return data;
}
function logo(){return `<a class="brand" href="${state.isAdmin?'/admin':'/dashboard'}" data-link aria-label="Bizflow.lk dashboard"><img class="sidebar-logo-full" src="/assets/bizflow-logo.png" alt="Bizflow.lk"><img class="sidebar-logo-icon" src="/assets/bizflow-icon.png" alt=""></a>`;}

const customerSections=[
  {label:'OVERVIEW',items:[['Dashboard','/dashboard','grid']]},
  {label:'BUSINESS',items:[['Sales','/sales','bag'],['Products','/products','box'],['Inventory','/inventory','layers'],['Customers','/customers','users'],['Suppliers','/suppliers','truck']]},
  {label:'DOCUMENTS',items:[['Invoices','/invoices','receipt'],['Quotations','/quotations','quote'],['Expenses','/expenses','wallet']]},
  {label:'INSIGHTS',items:[['Reports','/reports','chart']]},
  {label:'WORKSPACE',items:[['Staff','/staff','users'],['Billing','/billing','wallet'],['Settings','/settings','settings']]},
];
const adminSections=[
  {label:'PLATFORM',items:[['Overview','/admin','grid'],['Customers','/admin/users','users'],['Businesses','/admin/businesses','building'],['Payment verification','/admin/billing','receipt']]},
  {label:'CONTROL',items:[['Plans & pricing','/admin/plans','layers'],['Audit log','/admin/audit-logs','file'],['Bank transfer details','/admin/settings/payment','wallet'],['Support information','/admin/settings/general','settings']]},
];
function roleCanSee(label){
  if(state.isAdmin)return true;
  const role=state.user?.membership_role||'OWNER';
  if(role==='OWNER')return true;
  if(label==='Staff'||label==='Settings'||label==='Billing')return false;
  if(role==='STAFF'&&['Suppliers','Invoices','Quotations','Expenses','Reports'].includes(label))return false;
  if(role==='MANAGER'&&['Staff','Billing','Settings'].includes(label))return false;
  return true;
}
function navSections(){return state.isAdmin?adminSections:customerSections;}
function sidebar(path){
  const sections=navSections();
  const collapsed=localStorage.getItem('bizflow_sidebar_collapsed')==='true';
  const links=sections.map(section=>`<div class="nav-label">${esc(section.label)}</div><nav class="side-nav">${section.items.filter(([label])=>roleCanSee(label)).map(([label,url,ico])=>`<a href="${url}" data-link ${collapsed?`title="${esc(label)}"`:''} class="${path===url||(url!=='/admin'&&path.startsWith(url+'/'))?'active':''}">${icon(ico)}<span>${esc(label)}</span>${!state.isAdmin&&label==='Billing'&&state.subscription?.status==='EXPIRED'?'<span class="nav-count">!</span>':''}</a>`).join('')}</nav>`).join('');
  const name=state.business?.business_name||'BizFlow workspace';
  const avatar=state.user?.avatar_url?`<img src="${esc(state.user.avatar_url)}" alt="">`:esc(initials(state.user?.full_name));
  const bottom=state.isAdmin?`<div class="user-mini"><div class="user-avatar">${avatar}</div><div class="user-mini-copy"><strong>${esc(state.user?.full_name||'Administrator')}</strong><span>Super administrator</span></div><button class="top-icon" data-action="logout" title="Sign out">${icon('logout')}</button></div>`:`<div class="upgrade-mini"><strong>${esc(state.currentPlan)} plan</strong><p>${state.currentPlan==='FREE'?'Keep using Free or explore plans when you need more.':'Manage your 30-day access and renewals.'}</p><a class="btn btn-soft" data-link href="${state.currentPlan==='FREE'?'/pricing':'/billing'}">${state.currentPlan==='FREE'?'Explore plans':'Manage billing'} ${icon('arrow')}</a></div><div class="user-mini"><div class="user-avatar">${avatar}</div><div class="user-mini-copy"><strong>${esc(state.user?.full_name||'Account')}</strong><span>${esc(state.user?.email||'')}</span></div><button class="top-icon" data-action="logout" title="Sign out">${icon('logout')}</button></div>`;
  return `<aside class="sidebar ${collapsed?'collapsed':''}" id="sidebar"><div class="sidebar-brand">${logo()}<button class="sidebar-collapse" data-action="toggle-sidebar-collapse" title="${collapsed?'Expand':'Collapse'} navigation" aria-label="${collapsed?'Expand':'Collapse'} navigation" aria-pressed="${collapsed}">${icon('collapse')}</button></div>${!state.isAdmin?`<div class="workspace-switch"><div class="workspace-avatar">${esc(initials(name))}</div><div class="workspace-copy"><strong>${esc(name)}</strong><span>${esc(state.user?.membership_role||'OWNER')} WORKSPACE</span></div>${icon('grid')}</div>`:''}<div class="sidebar-scroll">${links}${state.isAdmin?`<div class="nav-label">SHORTCUT</div><nav class="side-nav"><a href="/admin/users/create" data-link class="${path==='/admin/users/create'?'active':''}">${icon('plus')}<span>Create customer account</span></a></nav>`:''}</div><div class="sidebar-bottom">${bottom}</div></aside>`;
}
function headingTitle(path){
  const map={'/dashboard':'Dashboard','/products':'Products','/inventory':'Inventory','/sales':'Sales','/customers':'Customers','/suppliers':'Suppliers','/invoices':'Invoices','/quotations':'Quotations','/expenses':'Expenses','/reports':'Reports','/staff':'Staff','/billing':'Billing','/billing/upgrade':'Upgrade plan','/billing/history':'Payment history','/settings':'Settings','/settings/security':'Security settings','/account':'Account','/admin':'Admin overview','/admin/users':'Customers','/admin/users/create':'Create account','/admin/businesses':'Businesses','/admin/billing':'Payment verification','/admin/audit-logs':'Audit log','/admin/plans':'Plan configuration','/admin/settings/payment':'Bank transfer settings','/admin/settings/general':'Platform settings'};
  if(map[path])return map[path];if(path.startsWith('/admin/users/'))return 'Customer details';if(path.startsWith('/admin/businesses/'))return 'Business details';return 'BizFlow';
}
function shell(path, content){
  const name=state.user?.full_name||'Account';
  const collapsed=localStorage.getItem('bizflow_sidebar_collapsed')==='true';
  const avatar=state.user?.avatar_url?`<img src="${esc(state.user.avatar_url)}" alt="">`:esc(initials(name));
  return `<div class="app-shell ${collapsed?'sidebar-collapsed':''}">${sidebar(path)}<div class="mobile-overlay" id="mobile-overlay" data-action="toggle-sidebar"></div><div class="main-column"><header class="topbar"><div class="topbar-left"><button class="mobile-menu" data-action="toggle-sidebar" aria-label="Open navigation" aria-expanded="false">${icon('menu')}</button><div class="crumb">${state.isAdmin?'Platform':'Workspace'} <span style="color:#c6cec7">/</span> <strong>${esc(headingTitle(path))}</strong></div><button class="command-trigger" data-action="open-command" aria-label="Search pages and navigate, Control K">${icon('search')}<span>Search pages</span><kbd>Ctrl K</kbd></button></div><div class="topbar-right">${!state.isAdmin?`<span class="badge badge-${esc(state.currentPlan.toLowerCase())}">${esc(state.currentPlan)}</span>`:''}<button class="top-icon" data-action="notifications" aria-label="Notifications">${icon('bell')}<i class="notify-dot" id="notify-dot" style="display:none"></i></button><span class="top-divider"></span><button class="top-profile" data-action="profile-menu"><span class="user-avatar">${avatar}</span><span>${esc(name.split(' ')[0])}</span></button></div></header><main class="page-wrap" id="view-content">${content}</main></div></div><div id="modal-root"></div>`;
}
function loadingPage(){return `<div class="loading-box"><img class="loading-logo" src="/assets/bizflow-logo.png" alt="Bizflow.lk"><span>Loading your workspace…</span></div>`;}
let motionObserver=null;
function animateContent(root=appRoot){
  const page=root.matches?.('.page-wrap')?root:root.querySelector?.('.public-shell,.app-shell,.auth-page,.page-lock')||root.firstElementChild;
  if(page){page.classList.remove('page-enter');void page.offsetWidth;page.classList.add('page-enter');page.addEventListener('animationend',()=>page.classList.remove('page-enter'),{once:true});}
  motionObserver?.disconnect();
  const candidates=[...(root.matches?.('.feature-card,.price-card,.step,.contact-card,.faq-item,.stat-card,.chart-card,.billing-hero,.admin-banner,.upgrade-banner')?[root]:[]),...root.querySelectorAll('.feature-card,.price-card,.step,.contact-card,.faq-item,.stat-card,.chart-card,.billing-hero,.admin-banner,.upgrade-banner')];
  const reduced=window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  if(reduced||!('IntersectionObserver' in window)){candidates.forEach(item=>item.classList.add('is-visible'));return;}
  motionObserver=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){entry.target.classList.add('is-visible');motionObserver?.unobserve(entry.target);}}),{threshold:.12,rootMargin:'0px 0px -22px 0px'});
  candidates.forEach((item,index)=>{item.classList.add('reveal-item');item.style.setProperty('--reveal-delay',`${(index%4)*55}ms`);motionObserver.observe(item);});
}
window.addEventListener('scroll',()=>{document.querySelector('.public-header')?.classList.toggle('is-scrolled',window.scrollY>8);},{passive:true});
function passwordChangePage(){return `<main class="auth-page"><section class="auth-left"><a class="brand auth-brand" data-link href="/dashboard" aria-label="Bizflow.lk home"><span class="brand-logo-panel"><img class="brand-logo" src="/assets/bizflow-logo.png" alt="Bizflow.lk"></span></a><div class="auth-copy"><div class="eyebrow eyebrow-light"><span class="eyebrow-dot"></span>FIRST SIGN-IN</div><h1>One quick security step.</h1><p>Your administrator created a temporary password. Set a new password before entering your business workspace.</p></div><div class="auth-legal">BizFlow LK · secure business access</div></section><section class="auth-right"><div class="auth-card"><h2>Change temporary password</h2><p class="auth-sub">Choose a new password with at least 10 characters.</p><form class="form-stack" data-form="force-password"><div class="field"><label>Current temporary password</label><input name="current_password" type="password" required autocomplete="current-password"></div><div class="field"><label>New password</label><input name="new_password" type="password" required minlength="10" maxlength="128" autocomplete="new-password"></div><div class="field"><label>Confirm new password</label><input name="confirm_password" type="password" required minlength="10" maxlength="128" autocomplete="new-password"></div><button class="btn btn-primary">Save new password ${icon('arrow')}</button><button type="button" class="btn btn-ghost" data-action="logout">Sign out</button></form></div></section></main><div id="modal-root"></div>`;}
function registrationSuccessPage(result){
  const items=[['Username',result.username],['Account ID',result.public_account_id],['Email address',result.email]];
  return `<main class="auth-page"><section class="auth-left"><a class="brand auth-brand" href="/" data-link aria-label="Bizflow.lk home"><span class="brand-logo-panel"><img class="brand-logo" src="/assets/bizflow-logo.png" alt="Bizflow.lk"></span></a><div class="auth-copy"><div class="eyebrow eyebrow-light"><span class="eyebrow-dot"></span>YOUR FREE WORKSPACE IS READY</div><h1>Welcome to BizFlow LK.</h1><p>Your account starts on Free. Paid access remains separate and requires an approved bank-transfer request.</p></div><div class="auth-legal">Keep your username and account ID somewhere safe.</div></section><section class="auth-right"><div class="auth-card"><div class="lock-icon">${icon('check')}</div><h2>Account created</h2><p class="auth-sub">Save these sign-in details. Your username and account ID are permanent references.</p><div class="activity-list">${items.map(([label,value])=>`<div class="activity-row"><div class="activity-copy"><span>${esc(label)}</span><strong>${esc(value||'')}</strong></div><button class="btn btn-ghost btn-small" type="button" data-action="copy-registration-value" data-value="${esc(value||'')}">Copy</button></div>`).join('')}</div><div class="notice notice-info" style="margin-top:16px">${icon('shield')}<span>Your identity number is protected and will appear masked in your account settings.</span></div><a class="btn btn-primary" style="margin-top:18px;width:100%" href="/dashboard" data-link>Open your dashboard ${icon('arrow')}</a></div></section></main><div id="modal-root"></div>`;
}
function suspendedPage(){return `<main class="auth-page"><section class="auth-left"><a class="brand auth-brand" href="/" data-link aria-label="Bizflow.lk home"><span class="brand-logo-panel"><img class="brand-logo" src="/assets/bizflow-logo.png" alt="Bizflow.lk"></span></a><div class="auth-copy"><div class="eyebrow eyebrow-light"><span class="eyebrow-dot"></span>ACCOUNT ACCESS</div><h1>Let’s get this sorted.</h1><p>Your business records remain in place. Please contact BizFlow support about the current account status.</p></div><div class="auth-legal">© ${new Date().getFullYear()} BizFlow LK</div></section><section class="auth-right"><div class="auth-card"><div class="lock-icon">${icon('lock')}</div><h2>Your account has been suspended.</h2><p class="auth-sub">Normal business operations are temporarily unavailable. Your products, customers, sales and invoices have not been deleted.</p>${state.publicSettings?.support_email?`<p><a href="mailto:${esc(state.publicSettings.support_email)}">${esc(state.publicSettings.support_email)}</a></p>`:''}<button class="btn btn-primary" data-action="logout">Sign out</button></div></section></main><div id="modal-root"></div>`;}
function unauthorizedPage(){return `<div class="page-lock"><div class="lock-icon">${icon('shield')}</div><h2>This area is restricted</h2><p class="section-intro" style="margin:8px auto 18px">Only an authorized BizFlow Super Admin can access platform administration.</p><a class="btn btn-primary" data-link href="/dashboard">Back to your dashboard</a></div>`;}

async function loadNotifyDot(){
  if(state.isAdmin||!state.user)return;
  try{const d=await api('/api/notifications');const dot=document.getElementById('notify-dot');if(dot)dot.style.display=d.unread?'block':'none';}catch{}
}
async function render(){
  const version=++renderVersion;const path=location.pathname;state.pathname=path;updateSeo(path);
  try{
    if(publicPaths.has(path)){
      if(path==='/login'||path==='/register'||path==='/forgot-password')appRoot.innerHTML=renderAuth(path);
      else appRoot.innerHTML=await renderPublic(path);
      animateContent(appRoot);
      return;
    }
    if(!state.user){
      appRoot.innerHTML=renderAuth('/login');
      return;
    }
    if(state.user.must_change_password){appRoot.innerHTML=passwordChangePage();return;}
    if(state.user.account_status==='SUSPENDED'&&!state.isAdmin){appRoot.innerHTML=suspendedPage();return;}
    if(path==='/settings/security'){
      appRoot.innerHTML=shell(path,loadingPage());
      try{const content=await renderPage(path);if(version!==renderVersion)return;document.getElementById('view-content').innerHTML=content;animateContent(document.getElementById('view-content'));}
      catch(error){if(version!==renderVersion)return;if(error.status===401){clearSession();appRoot.innerHTML=renderAuth('/login');return;}document.getElementById('view-content').innerHTML=errorPanel(error);}
      return;
    }
    if(state.isAdmin){
      if(!path.startsWith('/admin')){navigate('/admin',{replace:true});return;}
      appRoot.innerHTML=shell(path,loadingPage());
      try{
        const content=await renderAdmin(path);
        if(version!==renderVersion)return;
        document.getElementById('view-content').innerHTML=content;
        animateContent(document.getElementById('view-content'));
        loadNotifyDot();
      }catch(error){if(version!==renderVersion)return;document.getElementById('view-content').innerHTML=errorPanel(error,'/admin');}
      return;
    }
    if(path.startsWith('/admin')){appRoot.innerHTML=shell(path,unauthorizedPage());return;}
    appRoot.innerHTML=shell(path,loadingPage());
    try{
      const content=await renderPage(path);
      if(version!==renderVersion)return;
      document.getElementById('view-content').innerHTML=content;
      animateContent(document.getElementById('view-content'));
      if(location.hash){setTimeout(()=>document.getElementById(decodeURIComponent(location.hash.slice(1)))?.scrollIntoView({behavior:'smooth'}),100);}
      loadNotifyDot();
    }catch(error){
      if(version!==renderVersion)return;
      if(error.status===401){clearSession();appRoot.innerHTML=renderAuth('/login');return;}
      document.getElementById('view-content').innerHTML=errorPanel(error);
    }
  }catch(error){appRoot.innerHTML=renderAuth('/login');}
}

function navTo(href){
  const url=new URL(href,location.origin);const next=url.pathname+url.search+url.hash;
  if(url.origin!==location.origin)return;
  const reduced=window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  if(url.hash&&url.pathname===location.pathname){
    history.pushState({},'',next);
    document.getElementById(decodeURIComponent(url.hash.slice(1)))?.scrollIntoView({behavior:reduced?'auto':'smooth'});
    document.getElementById('public-nav')?.classList.remove('open');
    document.querySelector('.public-mobile-toggle')?.setAttribute('aria-expanded','false');
    return;
  }
  if(url.pathname==='/'&&url.hash){
    navigate(next);
    window.setTimeout(()=>document.getElementById(decodeURIComponent(url.hash.slice(1)))?.scrollIntoView({behavior:reduced?'auto':'smooth'}),120);
    return;
  }
  navigate(next);
}
function formValues(form){return Object.fromEntries(new FormData(form).entries());}
function planLandingAction(plan){
  if(plan==='FREE'){sessionStorage.removeItem('bizflow_pending_plan');navigate(state.user?(state.isAdmin?'/admin':'/dashboard'):'/register');return;}
  if(state.user){navigate(state.isAdmin?'/admin':`/billing/upgrade?plan=${encodeURIComponent(plan)}`);return;}
  sessionStorage.setItem('bizflow_pending_plan',plan);navigate('/register');
}
async function handleAuthForm(form){
  const kind=form.dataset.form;const data=formValues(form);
  if(kind==='login'||kind==='register'){
    const button=form.querySelector('button[type="submit"]');const old=button?.textContent;if(button){button.disabled=true;button.innerHTML=`<span class="spinner"></span>${kind==='register'?'Creating account…':'Signing in…'}`;}
    if(kind==='register'&&data.password!==data.confirm_password){toast('The passwords do not match.','error');if(button){button.disabled=false;button.textContent=old||'Create free account';}return;}
    try{
      const endpoint=kind==='login'?'/api/auth/login':'/api/auth/register';
      const result=await api(endpoint,{method:'POST',body:data});state.csrf=result.csrf_token||null;
      const me=await loadSession();
      if(kind==='register'){
        sessionStorage.removeItem('bizflow_pending_plan');
        appRoot.innerHTML=registrationSuccessPage(result);animateContent(appRoot);return;
      }
      const pending=sessionStorage.getItem('bizflow_pending_plan');sessionStorage.removeItem('bizflow_pending_plan');
      if(pending&&pending!=='FREE'&&!state.isAdmin&&!me.must_change_password)navigate(`/billing/upgrade?plan=${encodeURIComponent(pending)}`);
      else if(me.must_change_password)navigate('/dashboard');
      else navigate(me.is_admin?'/admin':'/dashboard');
    }catch(error){toast(error.message,'error');if(button){button.disabled=false;button.textContent=old||'Continue';}}
    return;
  }
  if(kind==='force-password'){
    if(data.new_password!==data.confirm_password){toast('The new passwords do not match.','error');return;}
    try{const r=await api('/api/auth/change-password',{method:'POST',body:data});toast(r.message);const me=await loadSession();if(me.is_admin)navigate('/admin');else navigate('/dashboard');}
    catch(error){toast(error.message,'error');}
  }
}
async function handleFormSubmit(event){
  const form=event.target.closest('form[data-form]');if(!form)return;
  event.preventDefault();
  if(['login','register','force-password'].includes(form.dataset.form)){await handleAuthForm(form);return;}
  const button=form.querySelector('button[type="submit"],button:not([type])');const original=button?.innerHTML;if(button){button.disabled=true;button.innerHTML='<span class="spinner"></span>Saving…';}
  try{
    const handled=state.isAdmin?await handleAdminForm(form):await handleForm(form);
    if(!handled&&button){button.disabled=false;button.innerHTML=original;}
  }catch(error){
    if(error.status===401){clearSession();navigate('/login');}
    else if(error.status===402){toast(error.message,'warning');showUpgradeModal(error.message);}
    else toast(error.message||'We couldn’t save this information. Please check the form and try again.','error');
    if(button){button.disabled=false;button.innerHTML=original;}
  }
}
function showUpgradeModal(message){
  modal('Your current plan limit',message||'Choose a plan that includes this feature.','<div class="notice notice-info"><span>BizFlow checks plan limits on the server. Your saved business data is unchanged.</span></div>',`<button class="btn btn-ghost" data-action="close-modal">Keep using Free</button><a class="btn btn-primary" data-link href="/pricing">View plans ${icon('arrow')}</a>`);
}
function openCommandPalette(){
  const items=state.isAdmin?[
    ['Platform overview','/admin','grid','Overview'],['Customer accounts','/admin/users','users','Platform'],['Businesses','/admin/businesses','building','Platform'],['Payment verification','/admin/billing','receipt','Platform'],['Plans & pricing','/admin/plans','layers','Control'],['Audit log','/admin/audit-logs','file','Control'],['Bank transfer details','/admin/settings/payment','wallet','Control'],['Platform settings','/admin/settings/general','settings','Control'],
  ]:[
    ['Dashboard','/dashboard','grid','Overview'],['Sales & point of sale','/sales','bag','Business'],['Products','/products','box','Business'],['Inventory','/inventory','layers','Business'],['Customers','/customers','users','Business'],['Suppliers','/suppliers','truck','Business'],['Invoices','/invoices','receipt','Documents'],['Quotations','/quotations','quote','Documents'],['Expenses','/expenses','wallet','Documents'],['Reports','/reports','chart','Insights'],['Staff','/staff','users','Workspace'],['Billing','/billing','wallet','Workspace'],['Settings','/settings','settings','Workspace'],['Account profile','/account','user','Account'],
  ];
  const list=items.map(([label,path,ico,section],index)=>`<a class="command-item ${index===0?'selected':''}" href="${path}" data-link data-command-item data-search="${esc(`${label} ${section}`.toLowerCase())}" role="option" aria-selected="${index===0}">${icon(ico)}<span><strong>${esc(label)}</strong><small>${esc(section)}</small></span><kbd>↵</kbd></a>`).join('');
  modal('Quick navigation','Find a page in your workspace',`<div class="command-search">${icon('search')}<input type="search" data-command-filter placeholder="Search pages…" autocomplete="off" aria-label="Search pages"></div><div class="command-results" role="listbox" aria-label="Workspace pages">${list}<p class="command-no-results" hidden>No matching pages. Try another search.</p></div>`,`<span class="command-hint"><kbd>↑</kbd><kbd>↓</kbd> to move <kbd>Enter</kbd> to open <kbd>Esc</kbd> to close</span>`);
}
function filterCommandPalette(input){
  const query=input.value.trim().toLowerCase();const items=[...document.querySelectorAll('[data-command-item]')];let first=null;
  items.forEach(item=>{const visible=item.dataset.search.includes(query);item.hidden=!visible;item.classList.remove('selected');item.setAttribute('aria-selected','false');if(visible&&!first)first=item;});
  first?.classList.add('selected');first?.setAttribute('aria-selected','true');
  const empty=document.querySelector('.command-no-results');if(empty)empty.hidden=Boolean(first);
}
async function handleGlobalAction(action,el){
  if(action==='copy-temp-password'){
    const field=document.getElementById('generated-password');
    if(field){try{if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(field.value);else{field.select();document.execCommand('copy');}toast('Temporary password copied.');}catch{toast('Could not copy automatically.','error');}}
    return true;
  }
  if(action==='copy-registration-value'){
    const value=el.dataset.value||'';
    try{
      if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(value);
      else{const box=document.createElement('textarea');box.value=value;document.body.appendChild(box);box.select();document.execCommand('copy');box.remove();}
      toast('Copied to clipboard.');
    }catch{toast('Could not copy automatically. Please select and copy the value.','error');}
    return true;
  }
  if(action==='recovery-whatsapp'){
    const form=el.closest('form');const data=form?formValues(form):{};const s=state.publicSettings||{};
    const message=`${s.support_message||'Please help me recover access to my BizFlow account.'}\n\nAccount ID: ${data.account_id||''}\nUsername: ${data.username||''}\nName: ${data.full_name||''}\nEmail: ${data.email||''}\nPhone: ${data.phone||''}`;
    let phone=String(el.dataset.whatsapp||s.support_whatsapp||s.whatsapp_number||'').replace(/\D/g,'');
    if(phone.startsWith('0'))phone=`94${phone.slice(1)}`;
    if(!phone){toast('WhatsApp support is not configured.','error');return true;}
    if(!form?.reportValidity())return true;
    window.open(`https://wa.me/${phone}?text=${encodeURIComponent(message)}`,'_blank','noopener,noreferrer');
    return true;
  }
  if(action==='open-command'){openCommandPalette();return true;}
  if(action==='close-modal'){closeModal();return true;}
  if(action==='retry-page'){if(publicPaths.has(location.pathname)){state.publicPlans=null;state.publicSettings=null;state.publicPlansUnavailable=false;}window.dispatchEvent(new Event('bf:refresh'));return true;}
  if(action==='toggle-sidebar'){const open=document.getElementById('sidebar')?.classList.toggle('open');document.getElementById('mobile-overlay')?.classList.toggle('visible',Boolean(open));document.querySelector('.mobile-menu')?.setAttribute('aria-expanded',String(Boolean(open)));return true;}
  if(action==='toggle-sidebar-collapse'){
    const sidebar=document.getElementById('sidebar');if(!sidebar)return true;
    const collapsed=sidebar.classList.toggle('collapsed');
    document.querySelector('.app-shell')?.classList.toggle('sidebar-collapsed',collapsed);
    localStorage.setItem('bizflow_sidebar_collapsed',String(collapsed));
    el.setAttribute('aria-pressed',String(collapsed));el.setAttribute('aria-label',`${collapsed?'Expand':'Collapse'} navigation`);el.title=`${collapsed?'Expand':'Collapse'} navigation`;
    sidebar.querySelectorAll('.side-nav a').forEach(link=>{const label=link.querySelector('span')?.textContent?.trim();if(collapsed&&label)link.title=label;else link.removeAttribute('title');});
    return true;
  }
  if(action==='logout'){
    try{await api('/api/auth/logout',{method:'POST',body:{}});}catch{}
    clearSession();state.cart=[];closeModal();navigate('/');return true;
  }
  if(action==='profile-menu'){
    modal('Your account',state.user?.email||'',`<div class="activity-list"><a class="activity-row" data-link href="${state.isAdmin?'/settings/security':'/account'}"><span class="activity-bullet">${icon('user')}</span><div class="activity-copy"><strong>${state.isAdmin?'Security settings':'Profile & security'}</strong><span>${state.isAdmin?'Change your administrator password':'Manage your profile and password'}</span></div></a>${!state.isAdmin?`<a class="activity-row" data-link href="/billing"><span class="activity-bullet">${icon('wallet')}</span><div class="activity-copy"><strong>Billing</strong><span>${esc(state.currentPlan)} plan</span></div></a>`:''}</div>`,`<button class="btn btn-ghost" data-action="close-modal">Close</button><button class="btn btn-danger" data-action="logout">${icon('logout')}Sign out</button>`);return true;
  }
  if(action==='notifications'){
    if(state.isAdmin){modal('Administrator notifications','Payment and subscription operations are available from the admin dashboard.',`<a class="activity-row" data-link href="/admin/billing"><span class="activity-bullet">${icon('receipt')}</span><div class="activity-copy"><strong>Payment verification</strong><span>Review pending bank transfer requests.</span></div></a>`);return true;}
    const d=await api('/api/notifications');const rows=d.items?.length?d.items.map(n=>`<div class="activity-row" style="opacity:${n.read_at?'.64':'1'}"><span class="activity-bullet">${icon(n.notification_type.includes('EXPIRED')||n.notification_type.includes('EXPIRING')?'clock':'bell')}</span><div class="activity-copy"><strong>${esc(n.message)}</strong><span>${dateFmt(n.created_at,true)}</span></div>${n.read_at?'':`<button class="btn btn-ghost btn-small" data-action="read-notification" data-id="${esc(n.id)}">Mark read</button>`}</div>`).join(''):emptyState('You’re all caught up','Payment and subscription updates will appear here.');
    modal('Notifications',`${d.unread||0} unread`,`<div class="activity-list">${rows}</div>`,`<button class="btn btn-ghost" data-action="close-modal">Close</button>${d.unread?'<button class="btn btn-primary" data-action="read-all">Mark all read</button>':''}`);return true;
  }
  if(action==='read-all'){await api('/api/notifications/read-all',{method:'POST',body:{}});closeModal();toast('Notifications marked as read.');return true;}
  if(action==='read-notification'){await api(`/api/notifications/${encodeURIComponent(el.dataset.id)}/read`,{method:'POST',body:{}});closeModal();toast('Notification marked as read.');return true;}
  if(action==='choose-plan'){planLandingAction(el.dataset.plan);return true;}
  if(action==='toggle-public-menu'||action==='public-menu'){
    const nav=document.getElementById('public-nav');const open=nav?.classList.toggle('open');
    el.setAttribute('aria-expanded',String(Boolean(open)));el.setAttribute('aria-label',open?'Close menu':'Open menu');return true;
  }
  return false;
}

appRoot.addEventListener('click',async event=>{
  const anchor=event.target.closest('a[data-link]');
  if(anchor){event.preventDefault();closeModal();navTo(anchor.href);return;}
  const el=event.target.closest('[data-action]');if(!el)return;
  const action=el.dataset.action;
  try{
    if(await handleGlobalAction(action,el))return;
    if(state.isAdmin){if(await handleAdminAction(action,el))return;}
    else {if(await handleAction(action,el))return;}
  }catch(error){if(error.status===402){toast(error.message,'warning');showUpgradeModal(error.message);}else toast(error.message||'That action could not be completed.','error');}
});
appRoot.addEventListener('submit',handleFormSubmit);
appRoot.addEventListener('input',event=>{
  const el=event.target;
  if(el.matches('[data-command-filter]')){filterCommandPalette(el);return;}
  if(el.matches('[data-admin-user-filter]')){handleAdminUserFilter(el);return;}
  if(el.matches('[data-admin-filter="search"],[data-admin-filter="plan"],[data-admin-filter="status"],[data-admin-filter="account_status"],[data-admin-filter="subscription_status"],[data-admin-filter="plan_id"],[data-admin-filter="date_from"],[data-admin-filter="date_to"]')){handleAdminFilter(el);return;}
  handleInput(el);
});
appRoot.addEventListener('change',event=>{
  const el=event.target;
  if(el.matches('[data-admin-user-filter]')){handleAdminUserFilter(el);return;}
  if(el.matches('[data-admin-filter]')){handleAdminFilter(el);return;}
  handleChange(el);
});
appRoot.addEventListener('keydown',event=>{
  if(!event.target.matches('[data-command-filter]'))return;
  const options=[...document.querySelectorAll('[data-command-item]:not([hidden])')];if(!options.length)return;
  const current=options.findIndex(x=>x.classList.contains('selected'));
  if(event.key==='ArrowDown'||event.key==='ArrowUp'){
    event.preventDefault();const next=(current+(event.key==='ArrowDown'?1:-1)+options.length)%options.length;
    options.forEach((item,index)=>{item.classList.toggle('selected',index===next);item.setAttribute('aria-selected',String(index===next));});
  }else if(event.key==='Enter'){
    event.preventDefault();(options.find(x=>x.classList.contains('selected'))||options[0]).click();
  }
});
window.addEventListener('popstate',()=>{closeModal();render();});
window.addEventListener('bf:refresh',()=>render());
window.addEventListener('keydown',event=>{
  if(event.key==='Escape')closeModal();
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'&&!publicPaths.has(location.pathname)){
    event.preventDefault();openCommandPalette();
  }
});

async function boot(){
  try{await loadSession();}catch{clearSession();}
  try{state.publicSettings=await api('/api/public/settings');}catch{}
  await render();
  const pending=sessionStorage.getItem('bizflow_pending_plan');
  if(pending&&state.user&&!state.isAdmin&&location.pathname==='/dashboard'){sessionStorage.removeItem('bizflow_pending_plan');navigate(`/billing/upgrade?plan=${encodeURIComponent(pending)}`);}
}
boot();
