(function(){
  const cart=[];
  const csrf=()=>document.querySelector('meta[name="csrf-token"]')?.content || '';
  const post=(url,body,headers={})=>fetch(url,{method:'POST',headers:{'X-CSRFToken':csrf(),...headers},body});
  window.addProduct=function(id,name){
    const found=cart.find(x=>x.product_id===id); if(found) found.quantity++; else cart.push({product_id:id,name,quantity:1}); renderCart();
  };
  function renderCart(){
    const el=document.getElementById('cart'); if(!el)return;
    if(!cart.length){el.textContent='Your cart is empty.';return;}
    el.innerHTML=cart.map((x,i)=>`<div class="cart-row"><span>${escapeHtml(x.name)} × ${x.quantity}</span><button type="button" data-remove="${i}">Remove</button></div>`).join('');
    el.querySelectorAll('[data-remove]').forEach(b=>b.addEventListener('click',()=>{cart.splice(Number(b.dataset.remove),1);renderCart();}));
  }
  function escapeHtml(s){return String(s).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
  window.chooseService=function(id){const s=document.getElementById('serviceSelect');if(s){s.value=String(id);s.scrollIntoView({behavior:'smooth',block:'center'});}};
  async function submitOrder(){
    const result=document.getElementById('orderResult');
    const payload={customer_name:document.getElementById('orderName')?.value,phone:document.getElementById('orderPhone')?.value,delivery_area:document.getElementById('orderArea')?.value,notes:document.getElementById('orderNotes')?.value,items:cart.map(x=>({product_id:x.product_id,quantity:x.quantity}))};
    if(!payload.items.length){result.textContent='Add at least one product to your order.';return;}
    try{const r=await post('/api/orders',JSON.stringify(payload),{'Content-Type':'application/json'});const d=await r.json();result.textContent=d.error||d.message||'Request failed.';if(r.ok){cart.length=0;renderCart();}}catch(e){result.textContent='Network error. Please try again.';}
  }
  async function submitService(){
    const result=document.getElementById('serviceResult');
    const payload={service_id:document.getElementById('serviceSelect')?.value,customer_name:document.getElementById('serviceName')?.value,phone:document.getElementById('servicePhone')?.value,area:document.getElementById('serviceArea')?.value,description:document.getElementById('serviceDescription')?.value};
    try{const r=await post('/api/service-requests',JSON.stringify(payload),{'Content-Type':'application/json'});const d=await r.json();result.textContent=d.error||d.message||'Request failed.';}catch(e){result.textContent='Network error. Please try again.';}
  }
  document.getElementById('submitOrder')?.addEventListener('click',submitOrder);
  document.getElementById('submitService')?.addEventListener('click',submitService);
  document.querySelectorAll('.add-product').forEach(b=>b.addEventListener('click',()=>addProduct(Number(b.dataset.id),b.dataset.name)));
  document.querySelectorAll('.choose-service').forEach(b=>b.addEventListener('click',()=>chooseService(Number(b.dataset.id))));
  async function sendForm(form,url,resultId){
    const result=document.getElementById(resultId);try{const r=await post(url,new FormData(form));const d=await r.json().catch(()=>({message:r.ok?'Done':'Request failed'}));result.textContent=d.error||d.message||(r.ok?'Done':'Request failed');return r;}catch(e){result.textContent='Network error. Please try again.';return null;}
  }
  const register=document.getElementById('register');if(register)register.addEventListener('submit',async e=>{e.preventDefault();await sendForm(e.target,register.dataset.url,'result');});
  const login=document.getElementById('login');if(login)login.addEventListener('submit',async e=>{e.preventDefault();const r=await sendForm(e.target,login.dataset.url,'result');if(r?.ok)location.href='/';});
  const resetRequest=document.getElementById('reset-request');if(resetRequest)resetRequest.addEventListener('submit',async e=>{e.preventDefault();await sendForm(e.target,resetRequest.dataset.url,'result');});
  const resetPassword=document.getElementById('reset-password');if(resetPassword)resetPassword.addEventListener('submit',async e=>{e.preventDefault();await sendForm(e.target,location.pathname,'result');});
})();
