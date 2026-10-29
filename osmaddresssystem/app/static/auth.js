/* Invited editor accounts. Session cookies are HttpOnly; only CSRF is held here. */
'use strict';
window.AuthUI=(()=>{
  const prefix=typeof P!=='undefined'?P:'';
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fr={
    'Sign in':'Se connecter','Sign out':'Se déconnecter','Username':'Identifiant','Passphrase':'Phrase secrète','Repeat passphrase':'Répéter la phrase secrète',
    'Display name':'Nom affiché','Create administrator':'Créer le compte administrateur','Set up this project':'Configurer ce projet',
    'Individual accounts keep every new edit attributed to its author.':'Les comptes individuels identifient l’auteur de chaque nouvelle modification.',
    'Use a passphrase of 15 to 128 characters.':'Utilisez une phrase secrète de 15 à 128 caractères.',
    'An administrator must set up accounts on this server first.':'Un administrateur doit d’abord configurer les comptes sur ce serveur.',
    'Ask your project administrator for an invitation or access reset.':'Demandez une invitation ou une réinitialisation à l’administrateur du projet.',
    'Activate your account':'Activer votre compte','Set your own passphrase':'Choisissez votre phrase secrète','Activate':'Activer',
    'Passphrases do not match.':'Les phrases secrètes ne correspondent pas.','Working…':'Traitement…',
    'Account':'Compte','History':'Historique','Team':'Équipe','Close':'Fermer','System':'Syst?me','Viewer':'Lecteur','Contributor':'Contributeur','Mapper':'Cartographe','Administrator':'Administrateur',
    'Your role does not allow this action.':'Votre rôle ne permet pas cette action.','Your session expired. Sign in again to continue.':'Votre session a expiré. Reconnectez-vous pour continuer.',
    'Manage project accounts':'Gérer les comptes du projet','Invite an editor':'Inviter un éditeur','Role':'Rôle','Create invitation':'Créer une invitation',
    'Invitation link':'Lien d’invitation','Share this link privately. It expires in 24 hours and can be used once.':'Partagez ce lien en privé. Il expire dans 24 heures et ne peut être utilisé qu’une fois.',
    'Copy link':'Copier le lien','Copied':'Copié','Copy the selected link.':'Copiez le lien sélectionné.','Enabled':'Activé','Pending activation':'Activation en attente',
    'Save access':'Enregistrer les droits','Reset access':'Réinitialiser l’accès','Access updated. Existing sessions were revoked.':'Droits mis à jour. Les sessions existantes ont été révoquées.',
    'Reset this account? Its current sessions and passphrase will stop working.':'Réinitialiser ce compte ? Ses sessions et sa phrase secrète seront invalidées.',
    'Change passphrase':'Changer la phrase secrète','Current passphrase':'Phrase secrète actuelle','New passphrase':'Nouvelle phrase secrète',
    'Passphrase changed. Sign in again.':'Phrase secrète modifiée. Reconnectez-vous.',
    'Sign out and discard this unfinished drawing?':'Se déconnecter et abandonner ce dessin inachevé ?',
    'Project history':'Historique du projet','Filter by editor, action or street':'Filtrer par auteur, action ou rue','Load older records':'Charger les entrées précédentes',
    'Before':'Avant','After':'Après','Details':'Détails','Unknown (legacy)':'Auteur inconnu (ancien)','No records yet.':'Aucune entrée pour le moment.',
    'Legacy record':'Ancienne entrée','Reversal':'Annulation / rétablissement','Edit':'Modification','Checkpoint':'Étape enregistrée',
    'The server must be restarted to enable editor accounts.':'Le serveur doit être redémarré pour activer les comptes.',
    'Restore reason (required, at least 5 characters):':'Motif de restauration (obligatoire, au moins 5 caractères) :'
  };
  const t=text=>I18n.language==='fr'?(Object.hasOwn(fr,text)?fr[text]:I18n.t(text)):text;
  let state={enabled:true,user:null,capabilities:[]},lastUser=null,invite=null,inviteUser=null;
  let active=false,readyDone=false;
  const fragment=new URLSearchParams(location.hash.slice(1));
  if(fragment.has('invite')){invite=fragment.get('invite');history.replaceState(null,'',location.pathname+location.search);}
  const can=cap=>state.enabled===false||state.capabilities?.includes(cap);
  const canView=()=>state.enabled===false||!!state.user;
  const headers=()=>state.csrf_token?{'X-CSRF-Token':state.csrf_token}:{};
  async function request(path,body){
    const response=await fetch(prefix+'api'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json',...headers()},body:JSON.stringify(body)});
    const data=await response.json();
    if(!response.ok){const e=new Error(typeof data.detail==='string'?data.detail:response.statusText);e.status=response.status;throw e;}
    return data;
  }
  const overlay=document.createElement('section');overlay.id='account-gate';overlay.hidden=true;document.body.appendChild(overlay);
  const dialog=document.createElement('dialog');dialog.id='account-dialog';document.body.appendChild(dialog);
  function freeze(on){document.querySelectorAll('body>header,body>nav,body>main,#modal').forEach(el=>el.inert=on);}
  function controls(){
    let bar=document.getElementById('account-controls');
    if(!bar){bar=document.createElement('div');bar.id='account-controls';document.querySelector('header')?.appendChild(bar);}
    bar.hidden=!state.enabled;
    if(!state.user){bar.textContent='';return;}
    bar.innerHTML=`<button id="account-profile" title="${esc(state.user.username)}">${esc(state.user.display_name)} <small>${esc(roleName(state.user.role))}</small></button>${can('audit')?`<button id="account-history">${t('History')}</button>`:''}${can('admin')?`<button id="account-team">${t('Team')}</button>`:''}<button id="account-logout">${t('Sign out')}</button>`;
    bar.querySelector('#account-profile').onclick=profile;
    if(can('audit'))bar.querySelector('#account-history').onclick=showHistory;
    if(can('admin'))bar.querySelector('#account-team').onclick=team;
    bar.querySelector('#account-logout').onclick=async()=>{
      if(typeof S!=='undefined'&&S.draw&&!confirm(t('Sign out and discard this unfinished drawing?')))return;
      try{await request('/auth/logout',{});location.reload();}catch(error){if(error.status===401)location.reload();else errorInDialog(error);}
    };
  }
  const roleName=role=>t(({viewer:'Viewer',contributor:'Contributor',mapper:'Mapper',admin:'Administrator'})[role]||role);
  function actionName(action){
    const labels={name:['Street name changed','Nom de rue modifié'],unname:['OSM name restored','Nom OSM restauré'],
      reference:['Reference point changed','Point de référence modifié'],settings:['Project settings changed','Paramètres modifiés'],
      access:['House access changed','Accès à une maison modifié'],unaccess:['House access reset','Accès réinitialisé'],
      road_geometry:['Road reshaped','Tracé de route modifié'],road_cut:['Road portion deleted','Portion de route supprimée'],road_join:['Roads connected','Routes raccordées'],
      roads_import:['Roads added','Routes ajoutées'],roads_remove:['Imported roads removed','Routes importées retirées'],
      edges_assign:['Street IDs changed','Identifiants modifiés'],edges_remove:['Road sections deleted','Tronçons supprimés'],edges_restore:['Road sections restored','Tronçons restaurés'],
      street_attrs:['Road attributes changed','Caractéristiques modifiées'],point_rank:['Landmark importance changed','Importance d’un repère modifiée'],
      road_connect:['Road connection changed','Connexion modifiée'],road_connect_many:['Road connections changed','Connexions modifiées'],road_unconnect:['Road disconnected','Route déconnectée'],
      restore:['Checkpoint restored','Étape restaurée'],checkpoint_saved:['Checkpoint saved','Étape enregistrée'],redo_branch_closed:['Previous redo branch closed','Ancienne branche de rétablissement fermée'],
      admin_created:['Administrator created','Administrateur créé'],account_invited:['Account invited','Compte invité'],account_activated:['Account activated','Compte activé'],
      signed_in:['Signed in','Connexion'],signed_out:['Signed out','Déconnexion'],account_updated:['Account access changed','Droits modifiés'],password_changed:['Passphrase changed','Phrase secrète modifiée'],
      local_account_recovery:['Account recovered locally','Compte récupéré localement'],computation_requested:['Calculation requested','Calcul demandé'],computation_finished:['Calculation completed','Calcul terminé']};
    return labels[action]?.[I18n.language==='fr'?1:0]||action;
  }
  function showGate(message=''){
    active=true;overlay.hidden=false;freeze(true);if(dialog.open)dialog.close();
    const setup=state.setup_required&&state.local_setup&&!invite;
    const unavailable=state.setup_required&&!state.local_setup&&!invite;
    const title=invite?'Activate your account':setup?'Set up this project':'Sign in';
    overlay.innerHTML=`<div class="account-card"><div class="account-language"><select id="account-language" aria-label="Language"><option value="en">English</option><option value="fr">Français</option></select></div><img class="account-logo" src="${prefix}static/brand/community-addressing-cameroon.png" alt="Community Addressing Cameroon"><h1>${t(title)}</h1><p>${t('Individual accounts keep every new edit attributed to its author.')}</p>${inviteUser?`<p><strong>${esc(inviteUser.display_name)}</strong> · ${esc(inviteUser.username)} · ${esc(roleName(inviteUser.role))}</p>`:''}${unavailable?`<p>${t('An administrator must set up accounts on this server first.')}</p>`:`<form id="account-login">${!invite?`<label>${t('Username')}<input id="account-username" name="username" autocomplete="username" required minlength="3" maxlength="40"></label>`:''}${setup?`<label>${t('Display name')}<input id="account-name" name="display_name" autocomplete="name" required maxlength="80"></label>`:''}<label>${t('Passphrase')}<input id="account-password" type="password" name="password" autocomplete="${setup||invite?'new-password':'current-password'}" required ${setup||invite?'minlength="15"':''} maxlength="128"></label>${setup||invite?`<label>${t('Repeat passphrase')}<input id="account-repeat" type="password" autocomplete="new-password" required minlength="15" maxlength="128"></label><small>${t('Use a passphrase of 15 to 128 characters.')}</small>`:''}<button class="primary" id="account-submit">${t(invite?'Activate':setup?'Create administrator':'Sign in')}</button></form>`}<p id="account-error" role="alert">${esc(t(message))}</p><small>${t('Ask your project administrator for an invitation or access reset.')}</small></div>`;
    const language=overlay.querySelector('#account-language');language.value=I18n.language;language.onchange=()=>I18n.setLanguage(language.value);
    overlay.querySelector('form')?.addEventListener('submit',async e=>{
      e.preventDefault();const button=overlay.querySelector('#account-submit');button.disabled=true;
      const password=overlay.querySelector('#account-password').value;
      try{
        if((setup||invite)&&password!==overlay.querySelector('#account-repeat').value)throw Error(t('Passphrases do not match.'));
        const body={password,...(invite?{token:invite}:{username:overlay.querySelector('#account-username').value}),...(setup?{display_name:overlay.querySelector('#account-name').value}:{})};
        const result=await request(invite?'/auth/activate':setup?'/auth/setup':'/auth/login',body);
        const changed=lastUser&&lastUser!==result.user.id;
        state=result;lastUser=result.user.id;invite=null;inviteUser=null;active=false;overlay.hidden=true;freeze(false);controls();applyPermissions();
        if(changed){location.reload();return;}
        window.dispatchEvent(new CustomEvent('authchange',{detail:{user:state.user}}));
      }catch(error){overlay.querySelector('#account-error').textContent=t(error.message);}finally{button.disabled=false;}
    });
  }
  function expired(){if(state.enabled===false)return;state.user=null;state.csrf_token=null;showGate('Your session expired. Sign in again to continue.');}
  function openDialog(title,html){
    dialog.innerHTML=`<div class="account-dialog-head"><h2>${esc(t(title))}</h2><button id="account-dialog-close" aria-label="${t('Close')}">×</button></div><div id="account-dialog-content">${html}</div>`;
    dialog.querySelector('#account-dialog-close').onclick=()=>dialog.close();
    if(!dialog.open)dialog.showModal();
  }
  function errorInDialog(error){if(error.status===401){expired();return;}if(!dialog.open)openDialog('Account','');let box=dialog.querySelector('.account-message');if(!box){box=document.createElement('p');box.className='account-message';dialog.appendChild(box);}box.textContent=t(error.message);}
  function invitationLink(token){
    const url=new URL(prefix||'./',location.href);url.hash='invite='+encodeURIComponent(token);
    const box=dialog.querySelector('#new-invitation');
    box.innerHTML=`<strong>${t('Invitation link')}</strong><p>${t('Share this link privately. It expires in 24 hours and can be used once.')}</p><input id="invitation-link" readonly aria-label="${t('Invitation link')}"><button id="copy-invitation">${t('Copy link')}</button>`;
    box.querySelector('input').value=url.href;
    box.querySelector('button').onclick=async()=>{try{await navigator.clipboard.writeText(url.href);box.querySelector('button').textContent=t('Copied');}catch(_){box.querySelector('input').select();box.querySelector('button').textContent=t('Copy the selected link.');}};
  }
  async function team(){
    try{
      const people=await request('/auth/users');
      const roleOptions=current=>Object.keys({contributor:1,mapper:1,viewer:1,admin:1}).map(r=>`<option value="${r}" ${r===current?'selected':''}>${roleName(r)}</option>`).join('');
      openDialog('Manage project accounts',`<form id="invite-form"><h3>${t('Invite an editor')}</h3><label>${t('Username')}<input name="username" required minlength="3" maxlength="40"></label><label>${t('Display name')}<input name="display_name" required maxlength="80"></label><label>${t('Role')}<select name="role">${roleOptions('contributor')}</select></label><button class="primary">${t('Create invitation')}</button></form><div id="new-invitation" class="new-invitation"></div><section>${people.map(u=>`<div class="account-person" data-person="${u.id}"><strong>${esc(u.display_name)}</strong><small>${esc(u.username)} · ${u.activated?'':t('Pending activation')}</small><div><select ${u.id===state.user.id?'disabled':''}>${roleOptions(u.role)}</select><label><input type="checkbox" ${u.enabled?'checked':''} ${u.id===state.user.id?'disabled':''}>${t('Enabled')}</label></div>${u.id!==state.user.id?`<button data-update="${u.id}">${t('Save access')}</button><button data-reset="${u.id}">${t('Reset access')}</button>`:''}</div>`).join('')}</section>`);
      dialog.querySelector('#invite-form').onsubmit=async e=>{e.preventDefault();try{const values=Object.fromEntries(new FormData(e.target));const result=await request('/auth/users',values);e.target.reset();invitationLink(result.activation_token);}catch(error){errorInDialog(error);}};
      dialog.querySelectorAll('[data-update],[data-reset]').forEach(button=>button.onclick=async()=>{
        const id=button.dataset.update||button.dataset.reset,row=button.closest('[data-person]'),reset=!!button.dataset.reset;
        if(reset&&!confirm(t('Reset this account? Its current sessions and passphrase will stop working.')))return;
        try{const result=await request('/auth/users/'+id,{role:row.querySelector('select').value,enabled:reset||row.querySelector('input').checked,reset});if(reset)invitationLink(result.activation_token);else button.textContent=t('Access updated. Existing sessions were revoked.');}catch(error){errorInDialog(error);}
      });
    }catch(error){errorInDialog(error);}
  }
  function profile(){
    openDialog('Change passphrase',`<form id="password-form"><p>${esc(state.user.display_name)} · ${esc(state.user.username)}</p><label>${t('Current passphrase')}<input name="current_password" type="password" autocomplete="current-password" required></label><label>${t('New passphrase')}<input name="password" type="password" autocomplete="new-password" required minlength="15" maxlength="128"></label><button class="primary">${t('Change passphrase')}</button></form>`);
    dialog.querySelector('form').onsubmit=async e=>{e.preventDefault();try{await request('/auth/password',Object.fromEntries(new FormData(e.target)));dialog.close();state.user=null;state.csrf_token=null;showGate('Passphrase changed. Sign in again.');}catch(error){errorInDialog(error);}};
  }
  async function showHistory(){
    let rows=[];
    const draw=()=>{
      const query=dialog.querySelector('#audit-filter').value.toLowerCase();
      const filtered=rows.filter(r=>JSON.stringify([r.details.display_name,r.details.username,actionName(r.action),r.action,r.street,r.kind]).toLowerCase().includes(query));
      dialog.querySelector('#audit-list').innerHTML=filtered.map(r=>`<article class="audit-record"><div><strong>${esc(r.actor?.startsWith('system:')?t('System'):r.actor?(r.details.display_name||r.actor):t('Unknown (legacy)'))}</strong><span>${esc(r.kind)} · ${esc(r.action)}</span></div><small>${esc(new Date(r.details.original_ts||r.ts).toLocaleString(I18n.language))} · #${r.id}${r.street!=null?' · '+esc(r.street):''}</small><details><summary>${t('Details')}</summary><p>${t('Before')}</p><pre>${esc(JSON.stringify(r.before,null,2))}</pre><p>${t('After')}</p><pre>${esc(JSON.stringify(r.after,null,2))}</pre><pre>${esc(JSON.stringify(r.details,null,2))}</pre></details></article>`).join('')||`<p>${t('No records yet.')}</p>`;
    };
    openDialog('Project history',`<input id="audit-filter" placeholder="${t('Filter by editor, action or street')}"><section id="audit-list"></section><button id="audit-more">${t('Load older records')}</button>`);
    const more=async()=>{try{const next=await request('/audit?limit=50'+(rows.length?'&before='+rows[rows.length-1].id:''));rows.push(...next);draw();dialog.querySelector('#audit-more').disabled=next.length<50;}catch(error){errorInDialog(error);}};
    dialog.querySelector('#audit-filter').oninput=draw;dialog.querySelector('#audit-more').onclick=more;await more();
  }
  function applyPermissions(){
    const guard=(selector,cap)=>document.querySelectorAll(selector).forEach(el=>{if(!can(cap)){el.disabled=true;el.dataset.roleDisabled='1';el.title=t('Your role does not allow this action.');}else if(el.dataset.roleDisabled){el.disabled=false;delete el.dataset.roleDisabled;}});
    guard('#btn-undo,#btn-redo','undo');guard('#btn-save','checkpoint');
    guard('#ref-move,#ref-yes,#ref-other,[data-act=apply-settings],[data-restore]','admin');
    guard('#name-input,#nm,#ok,[data-act=set-name],[data-act=delete-name],[data-act=clear-name]','names');
    guard('.id-change,[data-act^=draw-],[data-act^=reshape-],[data-act^=cut-],[data-act^=del-],#roads-file,#areas-create,[data-road-action=reshape],[data-road-action=cut],[data-road-action=whole],[data-road-action=join],[data-road-action=extend]','geometry');
    guard('.attr-edit,.pt-rank,#attrs-file','attributes');guard('[data-act=pick-access],[data-act=clear-access]','access');
    guard('#area-save','areas');guard('#recompute-now','compute');
  }
  let frame=0;
  new MutationObserver(records=>{
    if(!records.some(r=>[...r.addedNodes].some(n=>n.nodeType===1&&(n.matches('button,input,select')||n.querySelector('button,input,select')))))return;
    if(frame)return;frame=requestAnimationFrame(()=>{frame=0;applyPermissions();});
  }).observe(document.body,{subtree:true,childList:true});
  window.addEventListener('languagechange',()=>{
    controls();applyPermissions();
    if(active){const values={};overlay.querySelectorAll('input[id]').forEach(el=>values[el.id]=el.value);showGate();Object.entries(values).forEach(([id,value])=>{const el=document.getElementById(id);if(el)el.value=value;});}
  });
  const ready=(async()=>{
    try{
      state=await request('/auth/status');lastUser=state.user?.id||null;
      if(invite){inviteUser=await request('/auth/invite/preview',{token:invite});showGate();}
      else if(!canView())showGate();
      controls();return state;
    }catch(error){if(error.status===404){state={enabled:false,legacy:true};return state;}showGate(error.message);return state;}
    finally{readyDone=true;}
  })();
  return {ready,can,canView,headers,expired,applyPermissions,t,get state(){return state;},get readyDone(){return readyDone;}};
})();
