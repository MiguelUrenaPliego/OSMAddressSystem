"""Project-scoped invited accounts and revocable server-side sessions.

No external service or plaintext password storage. All persisted session and
invitation credentials are SHA256 verifiers of independent 256-bit secrets.
"""
from __future__ import annotations
import hashlib
import asyncio
import hmac
import ipaddress
import re
import secrets
import threading
import time
import uuid
from urllib.parse import urlsplit

import sqlalchemy as sa
from fastapi import APIRouter, Body, HTTPException, Request, Response

from .identity import identity

meta=sa.MetaData()
users=sa.Table('account_users',meta,sa.Column('id',sa.String(32),primary_key=True),
    sa.Column('username',sa.String(40),unique=True,nullable=False),sa.Column('display_name',sa.String(80),nullable=False),
    sa.Column('role',sa.String(16),nullable=False),sa.Column('enabled',sa.Boolean,nullable=False),
    sa.Column('password_hash',sa.Text),sa.Column('created',sa.Float,nullable=False))
sessions=sa.Table('account_sessions',meta,sa.Column('digest',sa.String(64),primary_key=True),
    sa.Column('user_id',sa.String(32),nullable=False,index=True),sa.Column('csrf',sa.String(64),nullable=False),
    sa.Column('expires',sa.Float,nullable=False),sa.Column('last_seen',sa.Float,nullable=False))
invites=sa.Table('account_invites',meta,sa.Column('digest',sa.String(64),primary_key=True),
    sa.Column('user_id',sa.String(32),nullable=False,index=True),sa.Column('expires',sa.Float,nullable=False))
attempts=sa.Table('account_attempts',meta,sa.Column('key',sa.String(64),primary_key=True),
    sa.Column('started',sa.Float,nullable=False),sa.Column('count',sa.Integer,nullable=False))

CAPS={
 'viewer':{'read','confirm'},
 'contributor':{'read','confirm','names','compute','undo','audit'},
 'mapper':{'read','confirm','names','compute','undo','audit','geometry','attributes','access','areas','checkpoint'},
 'admin':{'read','confirm','names','compute','undo','audit','geometry','attributes','access','areas','checkpoint','admin'},
}
POST_CAPS={
 '/api/names':'names','/api/compute':'compute','/api/addresses/compute':'compute',
 '/api/reference/confirm':'confirm','/api/reference':'admin','/api/settings':'admin','/api/restore':'admin',
 '/api/undo':'undo','/api/redo':'undo','/api/save':'checkpoint',
 '/api/access':'access','/api/access/snap':'access','/api/points/rank':'attributes','/api/streets/attrs':'attributes','/api/streets/attrs/import':'attributes','/api/streets/assign':'geometry',
 '/api/work-areas/grid':'geometry','/api/work-areas/status':'areas',
}
PUBLIC_AUTH={'/api/auth/status','/api/auth/setup','/api/auth/login','/api/auth/activate','/api/auth/invite/preview'}
MAX_AGE=8*3600
IDLE_AGE=30*60


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password):
    if not isinstance(password,str) or not 15<=len(password)<=128:
        raise HTTPException(400,'Use a passphrase of 15 to 128 characters.')
    salt=secrets.token_bytes(16)
    value=hashlib.scrypt(password.encode(),salt=salt,n=2**17,r=8,p=1,maxmem=256*1024*1024)
    return 'scrypt$'+salt.hex()+'$'+value.hex()


def verify_password(password,stored):
    if not isinstance(password,str) or len(password)>128: return False
    _,salt,expected=stored.split('$')
    actual=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=2**17,r=8,p=1,maxmem=256*1024*1024)
    return hmac.compare_digest(actual.hex(),expected)


def public_user(row):
    return {k:row[k] for k in ('id','username','display_name','role','enabled')}


def local_request(request):
    if any(request.headers.get(h) for h in ('forwarded','x-forwarded-for','x-forwarded-host','x-real-ip')):
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback and request.url.hostname in ('localhost','127.0.0.1','::1')
    except (ValueError,AttributeError): return False


def same_origin(request):
    origin=request.headers.get('origin')
    if origin:
        parsed=urlsplit(origin)
        if (parsed.scheme,parsed.netloc)!=(request.url.scheme,request.url.netloc):
            raise HTTPException(403,'Cross-origin writes are not allowed.')
    if request.headers.get('sec-fetch-site')=='cross-site':
        raise HTTPException(403,'Cross-site writes are not allowed.')
    if request.headers.get('content-type','').split(';')[0].strip().lower()!='application/json':
        raise HTTPException(415,'Send application/json.')


class Accounts:
    def __init__(self,store):
        self.store=store
        self.engine=store.engine
        self.lock=threading.RLock()
        self.write_lock=asyncio.Lock()
        self.hash_slots=threading.BoundedSemaphore(2)
        meta.create_all(self.engine)
        self.cookie='cac_session_'+digest(str(store.root.resolve()))[:12]
        # Equal work for an unknown username; never a usable account password.
        self.dummy=password_hash(secrets.token_urlsafe(32))

    def count(self):
        with self.engine.connect() as c:return c.execute(sa.select(sa.func.count()).select_from(users)).scalar_one()

    def resolve(self,request):
        token=request.cookies.get(self.cookie,'')
        if not token or len(token)>100:return None,None
        now=time.time()
        with self.engine.begin() as c:
            row=c.execute(sa.select(sessions).where(sessions.c.digest==digest(token))).mappings().first()
            if not row:return None,None
            if row['expires']<=now or row['last_seen']<=now-IDLE_AGE:
                c.execute(sessions.delete().where(sessions.c.digest==row['digest']));return None,None
            user=c.execute(sa.select(users).where(sa.and_(users.c.id==row['user_id'],users.c.enabled.is_(True),users.c.password_hash.is_not(None)))).mappings().first()
            if not user:return None,None
            if row['last_seen']<now-60:c.execute(sessions.update().where(sessions.c.digest==row['digest']).values(last_seen=now))
            return public_user(user),dict(row)

    def cookie_path(self,request):return (request.scope.get('root_path','').rstrip('/') or '')+'/'

    def session(self,request,response,user):
        token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        now=time.time()
        with self.engine.begin() as c:
            c.execute(sessions.delete().where(sessions.c.expires<now))
            c.execute(sessions.insert().values(digest=digest(token),user_id=user['id'],csrf=csrf,expires=now+MAX_AGE,last_seen=now))
        response.set_cookie(self.cookie,token,max_age=MAX_AGE,path=self.cookie_path(request),httponly=True,samesite='strict',
                            secure=request.url.scheme=='https' or not local_request(request))
        response.headers['Cache-Control']='no-store'
        return {'enabled':True,'user':public_user(user),'csrf_token':csrf,'capabilities':sorted(CAPS[user['role']])}

    def validate_user(self,body):
        if not isinstance(body.get('username'),str) or not isinstance(body.get('display_name'),str):
            raise HTTPException(400,'Username and display name are required.')
        username=str(body.get('username','')).strip().lower()
        name=str(body.get('display_name','')).strip()
        role=body.get('role','contributor')
        if not re.fullmatch(r'[a-z][a-z0-9_.-]{2,39}',username):raise HTTPException(400,'Username: 3-40 letters, digits, dots, underscores or hyphens; start with a letter.')
        if not 1<=len(name)<=80 or any(ord(ch)<32 for ch in name):raise HTTPException(400,'Enter a display name of 1-80 characters.')
        if not isinstance(role,str) or role not in CAPS:raise HTTPException(400,'Unknown account role.')
        return username,name,role

    def throttle(self,request,username):
        # Persistent counters, with independent account and actual-client buckets.
        now=time.time();host=request.client.host if request.client else 'unknown'
        keys=[digest('ip:'+host),digest('username:'+username)]
        with self.lock,self.engine.begin() as c:
            c.execute(attempts.delete().where(attempts.c.started<now-900))
            rows={r['key']:r for r in c.execute(sa.select(attempts).where(attempts.c.key.in_(keys))).mappings()}
            if any(r['count']>=8 for r in rows.values()):raise HTTPException(429,'Too many sign-in attempts. Try again in 15 minutes.')
            for key in keys:
                if key in rows:c.execute(attempts.update().where(attempts.c.key==key).values(count=attempts.c.count+1))
                else:c.execute(attempts.insert().values(key=key,started=now,count=1))

    def successful_attempt(self,request,username):
        host=request.client.host if request.client else 'unknown'
        with self.lock,self.engine.begin() as c:
            c.execute(attempts.delete().where(attempts.c.key==digest('username:'+username)))
            c.execute(attempts.update().where(sa.and_(attempts.c.key==digest('ip:'+host),attempts.c.count>0)).values(count=attempts.c.count-1))

    def bootstrap(self,username,name,password):
        """Also used by the local console recovery command, never by public signup."""
        username,name,_=self.validate_user({'username':username,'display_name':name,'role':'admin'})
        hashed=password_hash(password)
        with self.lock,self.engine.begin() as c:
            if c.execute(sa.select(sa.func.count()).select_from(users)).scalar_one():raise HTTPException(409,'Accounts already exist. Sign in or use the local recovery command.')
            user=dict(id=uuid.uuid4().hex,username=username,display_name=name,role='admin',enabled=True,password_hash=hashed,created=time.time())
            c.execute(users.insert().values(**user))
            self.store._audit(c,'account','admin_created',user['id'],details={'display_name':name,'username':username,'role':'admin','source':'local_setup'})
        return user

    def reset_local(self,username,name,password):
        username,name,_=self.validate_user({'username':username,'display_name':name,'role':'admin'})
        hashed=password_hash(password)
        with self.lock,self.engine.begin() as c:
            row=c.execute(sa.select(users).where(users.c.username==username)).mappings().first()
            if not row:raise HTTPException(404,'No such account.')
            c.execute(users.update().where(users.c.id==row['id']).values(password_hash=hashed,display_name=name,role='admin',enabled=True))
            c.execute(sessions.delete().where(sessions.c.user_id==row['id']))
            c.execute(invites.delete().where(invites.c.user_id==row['id']))
            self.store._audit(c,'account','local_account_recovery','system:console',details={'user_id':row['id'],'display_name':name})

    def require_admin(self,request):
        if not request.state.account or request.state.account['role']!='admin':raise HTTPException(403,'Administrator access required.')

    def router(self):
        router=APIRouter(prefix='/api/auth')

        @router.get('/status')
        def status(request:Request):
            user=request.state.account
            return {'enabled':True,'user':user,'csrf_token':request.state.session['csrf'] if user else None,
                    'capabilities':sorted(CAPS[user['role']]) if user else [],'setup_required':self.count()==0,
                    'local_setup':local_request(request)}

        @router.post('/setup')
        def setup(request:Request,response:Response,body:dict=Body(...)):
            if not local_request(request):raise HTTPException(403,'Initial setup is only available on localhost. Use the local account command on the server.')
            self.throttle(request,'setup')
            user=self.bootstrap(body.get('username'),body.get('display_name'),body.get('password'))
            self.successful_attempt(request,'setup')
            return self.session(request,response,user)

        @router.post('/login')
        def login(request:Request,response:Response,body:dict=Body(...)):
            username=str(body.get('username','')).strip().lower()[:128]
            self.throttle(request,username)
            with self.engine.connect() as c:user=c.execute(sa.select(users).where(users.c.username==username)).mappings().first()
            with self.hash_slots:
                valid=verify_password(body.get('password',''),user['password_hash'] if user and user['password_hash'] else self.dummy)
            if not valid or not user or not user['enabled']:raise HTTPException(401,'Incorrect username or passphrase.')
            self.successful_attempt(request,username)
            self.store.record_audit('signed_in',user['id'],display_name=user['display_name'],username=user['username'],role=user['role'],source='password_login')
            return self.session(request,response,user)

        def invitation(token):
            if not isinstance(token,str) or len(token)>100:raise HTTPException(400,'Invalid or expired invitation.')
            with self.engine.connect() as c:
                row=c.execute(sa.select(invites).where(invites.c.digest==digest(token))).mappings().first()
                user=c.execute(sa.select(users).where(users.c.id==row['user_id'])).mappings().first() if row else None
            if not row or row['expires']<time.time() or not user or not user['enabled']:raise HTTPException(400,'Invalid or expired invitation.')
            return row,user

        @router.post('/invite/preview')
        def preview(body:dict=Body(...)):
            _,user=invitation(body.get('token'))
            return {'username':user['username'],'display_name':user['display_name'],'role':user['role']}

        @router.post('/activate')
        def activate(request:Request,response:Response,body:dict=Body(...)):
            bucket='activation:'+digest(str(body.get('token',''))[:100])
            self.throttle(request,bucket)
            with self.lock:
                row,user=invitation(body.get('token'))
                with self.hash_slots:hashed=password_hash(body.get('password'))
                with self.engine.begin() as c:
                    deleted=c.execute(invites.delete().where(invites.c.digest==row['digest']))
                    if deleted.rowcount!=1:raise HTTPException(409,'Invitation already used.')
                    c.execute(users.update().where(users.c.id==user['id']).values(password_hash=hashed))
                    c.execute(sessions.delete().where(sessions.c.user_id==user['id']))
                    self.store._audit(c,'account','account_activated',user['id'],details={'display_name':user['display_name'],'username':user['username'],'role':user['role']})
            self.successful_attempt(request,bucket)
            return self.session(request,response,user)

        @router.post('/logout')
        def logout(request:Request,response:Response):
            with self.engine.begin() as c:
                c.execute(sessions.delete().where(sessions.c.digest==request.state.session['digest']))
                self.store._audit(c,'account','signed_out',request.state.account['id'])
            response.delete_cookie(self.cookie,path=self.cookie_path(request))
            return {'ok':True}

        @router.post('/password')
        def change_password(request:Request,body:dict=Body(...)):
            user=request.state.account
            self.throttle(request,user['username'])
            with self.engine.connect() as c:row=c.execute(sa.select(users).where(users.c.id==user['id'])).mappings().one()
            with self.hash_slots:
                if not verify_password(body.get('current_password',''),row['password_hash']):raise HTTPException(401,'Incorrect current passphrase.')
                hashed=password_hash(body.get('password'))
            with self.engine.begin() as c:
                c.execute(users.update().where(users.c.id==user['id']).values(password_hash=hashed))
                c.execute(sessions.delete().where(sessions.c.user_id==user['id']))
                self.store._audit(c,'account','password_changed',user['id'])
            self.successful_attempt(request,user['username'])
            return {'ok':True,'sign_in_required':True}

        @router.get('/users')
        def list_users(request:Request):
            self.require_admin(request)
            with self.engine.connect() as c:
                return [{**public_user(r),'activated':bool(r['password_hash'])} for r in c.execute(sa.select(users).order_by(users.c.username)).mappings()]

        @router.post('/users')
        def add_user(request:Request,body:dict=Body(...)):
            self.require_admin(request)
            username,name,role=self.validate_user(body)
            token=secrets.token_urlsafe(32);user_id=uuid.uuid4().hex
            with self.lock,self.engine.begin() as c:
                if c.execute(sa.select(users.c.id).where(users.c.username==username)).first():raise HTTPException(409,'Username already exists.')
                c.execute(users.insert().values(id=user_id,username=username,display_name=name,role=role,enabled=True,password_hash=None,created=time.time()))
                c.execute(invites.insert().values(digest=digest(token),user_id=user_id,expires=time.time()+86400))
                self.store._audit(c,'account','account_invited',request.state.account['id'],details={'target_user':user_id,'username':username,'assigned_role':role})
            return {'user_id':user_id,'activation_token':token,'expires_in':86400}

        @router.post('/users/{user_id}')
        def update_user(user_id:str,request:Request,body:dict=Body(...)):
            self.require_admin(request)
            if user_id==request.state.account['id']:raise HTTPException(409,'Another administrator must change your own role or access.')
            with self.lock,self.engine.begin() as c:
                row=c.execute(sa.select(users).where(users.c.id==user_id)).mappings().first()
                if not row:raise HTTPException(404,'No such account.')
                role=body.get('role',row['role']);enabled=body.get('enabled',row['enabled'])
                if not isinstance(role,str) or role not in CAPS or type(enabled) is not bool:raise HTTPException(400,'Choose a valid role and enabled status.')
                c.execute(users.update().where(users.c.id==user_id).values(role=role,enabled=enabled))
                c.execute(sessions.delete().where(sessions.c.user_id==user_id))
                token=None
                if body.get('reset') is True:
                    token=secrets.token_urlsafe(32)
                    c.execute(users.update().where(users.c.id==user_id).values(password_hash=None))
                    c.execute(invites.delete().where(invites.c.user_id==user_id))
                    c.execute(invites.insert().values(digest=digest(token),user_id=user_id,expires=time.time()+86400))
                self.store._audit(c,'account','account_updated',request.state.account['id'],before={'role':row['role'],'enabled':row['enabled']},after={'role':role,'enabled':enabled},details={'target_user':user_id,'reset':bool(token)})
            return {'ok':True,**({'activation_token':token,'expires_in':86400} if token else {})}

        return router

    async def protect(self,request,call_next):
        path=request.scope.get('path','').removeprefix(request.scope.get('root_path',''))
        # One project process: serialize input changes and revocations together.
        # Resolve identity AFTER acquiring the lock, so a queued write cannot
        # retain permissions that an administrator revoked while it waited.
        if request.method not in ('GET','HEAD','OPTIONS') and path not in ('/api/auth/login','/api/auth/invite/preview'):
            async with self.write_lock:
                return await self._protect(request,call_next)
        return await self._protect(request,call_next)

    async def _protect(self,request,call_next):
        request.state.account,request.state.session=self.resolve(request)
        actor=request.state.account
        token=identity.set(actor)
        path=request.scope.get('path','')
        root=request.scope.get('root_path','')
        if root and path.startswith(root):path=path[len(root):]
        try:
            if path.startswith('/api/'):
                if request.method not in ('GET','HEAD','OPTIONS'):
                    same_origin(request)
                if path not in PUBLIC_AUTH and path!='/api/health':
                    if not actor:raise HTTPException(401,'Sign in to this project.')
                    caps=CAPS[actor['role']]
                    if path in ('/api/audit','/api/history') and 'audit' not in caps:raise HTTPException(403,'History access requires an editor account.')
                    if request.method not in ('GET','HEAD','OPTIONS'):
                        supplied=request.headers.get('x-csrf-token','')
                        if not hmac.compare_digest(supplied.encode(),request.state.session['csrf'].encode()):raise HTTPException(403,'Session check failed. Refresh your sign-in.')
                        needed=POST_CAPS.get(path,'geometry' if path.startswith(('/api/roads/','/api/edges','/api/base/components/')) else 'admin')
                        if path in ('/api/auth/logout','/api/auth/password'):needed='read'
                        if needed not in caps:raise HTTPException(403,'Your role does not allow this action.')
            response=await call_next(request)
            if path.startswith('/api/'):
                response.headers['Cache-Control']='no-store'
                response.headers['X-Content-Type-Options']='nosniff'
            return response
        except HTTPException as exc:
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail':exc.detail},status_code=exc.status_code,headers={'Cache-Control':'no-store'})
        finally:identity.reset(token)
