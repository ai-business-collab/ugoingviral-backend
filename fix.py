lines = open('landing.html').readlines()
f1 = 'async function handleLogin(){'
f1 += 'var e=document.getElementById("login-email").value,'
f1 += 'p=document.getElementById("login-pass").value;'
f1 += 'if(!e||!p){alert("Udfyld felter");return;}'
f1 += 'fetch("/api/auth/login",{method:"POST",'
f1 += 'headers:{"Content-Type":"application/json"},'
f1 += 'body:JSON.stringify({email:e,password:p})})'
f1 += '.then(r=>r.json()).then(d=>{'
f1 += 'if(d.token){localStorage.setItem("ugv_token",d.token);'
f1 += 'window.location.href="/app";}else{alert(d.detail||"Fejl");}});}\n'
f2 = 'async function handleRegister(){'
f2 += 'var n=document.getElementById("reg-name").value,'
f2 += 'e=document.getElementById("reg-email").value,'
f2 += 'p=document.getElementById("reg-pass").value;'
f2 += 'if(!n||!e||!p){alert("Udfyld felter");return;}'
f2 += 'fetch("/api/auth/register",{method:"POST",'
f2 += 'headers:{"Content-Type":"application/json"},'
f2 += 'body:JSON.stringify({email:e,password:p,name:n})})'
f2 += '.then(r=>r.json()).then(d=>{'
f2 += 'if(d.token){localStorage.setItem("ugv_token",d.token);'
f2 += 'window.location.href="/app";}else{alert(d.detail||"Fejl");}});}\n'
lines.insert(821, f1)
lines.insert(822, f2)
open('landing.html', 'w').writelines(lines)
print('OK')
