import os, json
d = '/root/ugoingviral-backend/user_data'
for f in os.listdir(d):
    if not f.endswith('.json'): continue
    p = os.path.join(d, f)
    data = json.load(open(p))
    data['onboarding_completed'] = False
    open(p, 'w').write(json.dumps(data, indent=2))
    print('Reset:', f)
print('Faerdig')
