content = open('landing.html').read()

old1 = "function handleLogin() {\n  var email = document.getElementById('login-email').value;\n  var pass =
document.getElementById('login-pass').value;\n  if (!email || !pass) { alert('Udfyld email og adgangskode'); return;
}\n  // TODO: kald backend auth endpoint\n  alert('Login kommer snart \u2014 backend auth bygges i fase 08');\n}"

new1 = "async function handleLogin() {\n  var email = document.getElementById('login-email').value;\n  var pass =
document.getElementById('login-pass').value;\n  if (!email || !pass) { alert('Udfyld email og adgangskode'); return;
}\n  try {\n    var r = await fetch('/api/auth/login',
{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:email,password:pass})});\n
var d = await r.json();\n    if (d.token) { localStorage.setItem('ugv_token',d.token);
localStorage.setItem('ugv_user',JSON.stringify(d.user)); window.location.href='/app'; }\n    else { alert(d.detail ||
'Forkert email eller adgangskode'); }\n  } catch(e) { alert('Fejl \u2014 pr\u00f8v igen'); }\n}"

old2 = "function handleRegister() {\n  var name = document.getElementById('reg-name').value;\n  var email =
document.getElementById('reg-email').value;\n  var pass = document.getElementById('reg-pass').value;\n  if (!name ||
!email || !pass) { alert('Udfyld alle felter'); return; }\n  // TODO: kald backend register endpoint\n
alert('Registrering kommer snart \u2014 backend auth bygges i fase 08');\n}"

new2 = "async function handleRegister() {\n  var name = document.getElementById('reg-name').value;\n  var email =
document.getElementById('reg-email').value;\n  var pass = document.getElementById('reg-pass').value;\n  if (!name ||
!email || !pass) { alert('Udfyld alle felter'); return; }\n  try {\n    var r = await fetch('/api/auth/register', {met
hod:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:email,password:pass,name:name})});\n
    var d = await r.json();\n    if (d.token) { localStorage.setItem('ugv_token',d.token);
localStorage.setItem('ugv_user',JSON.stringify(d.user)); window.location.href='/app'; }\n    else { alert(d.detail ||
'Kunne ikke oprette konto'); }\n  } catch(e) { alert('Fejl \u2014 pr\u00f8v igen'); }\n}"

result = content.replace(old1, new1).replace(old2, new2)
open('landing.html', 'w').write(result)
print('Antal ændringer:', content.count('fase 08') - result.count('fase 08'))
