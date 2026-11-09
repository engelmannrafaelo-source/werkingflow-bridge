import subprocess,uuid,time,pathlib,json
root=pathlib.Path(__file__).resolve().parents[2]
sql=(root/'docker/migrations/077_schema_generator_account_reader.sql').read_text()
name='schema-reader-test-'+uuid.uuid4().hex[:10]
def run(args,data=None,check=True):
 r=subprocess.run(args,input=data,capture_output=True,text=True)
 if check and r.returncode:raise RuntimeError(r.stderr[:500])
 return r
def query(s,check=True):return run(['docker','exec','-i',name,'psql','-U','postgres','-v','ON_ERROR_STOP=1','-At'],s,check)
def test_enrollment_migration():
    try:
     run(['docker','run','-d','--rm','--network','none','--name',name,'-e','POSTGRES_HOST_AUTH_METHOD=trust','postgres:16-alpine'])
     for _ in range(30):
      if run(['docker','exec',name,'pg_isready','-h','127.0.0.1','-U','postgres'],check=False).returncode==0:break
      time.sleep(.2)
     setup="CREATE TABLE service_principals(id text,name text,allowed_apps text[],allowed_paths text[],active boolean);"
     query(setup);query(sql);assert query('SELECT count(*) FROM service_principals').stdout.strip()=='0'
     query("INSERT INTO service_principals VALUES ('keep','other',ARRAY['other'],ARRAY['*'],true),('target','schema-generator-dev',ARRAY['werking-schema-generator'],ARRAY['/v1/chat/completions'],true)")
     query(sql);query(sql)
     got=query("SELECT array_to_json(allowed_paths) FROM service_principals WHERE id='target'").stdout.strip();assert json.loads(got)==['/v1/chat/completions','/v1/auth/account-exists']
     assert query("SELECT array_to_json(allowed_paths) FROM service_principals WHERE id='keep'").stdout.strip()=='["*"]'
     query("UPDATE service_principals SET allowed_paths=ARRAY['*'] WHERE id='target'");assert query(sql,False).returncode!=0
     query("UPDATE service_principals SET allowed_paths=ARRAY['/v1/chat/completions'],active=false WHERE id='target'");assert query(sql,False).returncode!=0
     query("UPDATE service_principals SET active=true,allowed_apps=ARRAY['other'] WHERE id='target'");assert query(sql,False).returncode!=0
     print('PASS: absent no-op, exact additive grant, idempotency, other principal unchanged, wildcard/revoked/wrong-app rejected')
    finally:run(['docker','rm','-f',name],check=False)

if __name__ == '__main__': test_enrollment_migration()
