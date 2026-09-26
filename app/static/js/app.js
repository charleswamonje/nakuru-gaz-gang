(function(){
  const cart = [];

  const csrf = () =>
    document.querySelector('meta[name="csrf-token"]')?.content || '';

  const post = (url, body, headers = {}) =>
    fetch(url, {
      method: 'POST',
      headers: {'X-CSRFToken': csrf(), ...headers},
      body
    });

  window.addProduct = function(id, name, price, unit){
    const found = cart.find(x => x.product_id === id);

    if(found){
      found.quantity++;
    } else {
      cart.push({
        product_id: id,
        name: name,
        price: Number(price) || 0,
        unit: unit || '',
        quantity: 1
      });
    }

    renderCart();
  };

  function renderCart(){
    const el = document.getElementById('cart');
    if(!el) return;

    if(!cart.length){
      el.textContent = 'Your cart is empty.';
      return;
    }

    el.innerHTML = cart.map((x,i) => {
      const subtotal = x.price * x.quantity;

      return `
        <div class="cart-row">
          <div>
            <strong>${escapeHtml(x.name)}</strong>
            <div>${x.price > 0 ? 'KSh ' + x.price.toFixed(2) : 'Price confirmed before service'} ${escapeHtml(x.unit ? '/ ' + x.unit : '')}</div>
          </div>

          <div class="cart-controls">
            <button type="button" data-minus="${i}">−</button>
            <strong>${x.quantity}</strong>
            <button type="button" data-plus="${i}">+</button>
            <button type="button" data-remove="${i}">Remove</button>
          </div>

          ${
            x.price > 0
              ? `<strong>KSh ${subtotal.toFixed(2)}</strong>`
              : ''
          }
        </div>
      `;
    }).join('');

    el.querySelectorAll('[data-minus]').forEach(b =>
      b.addEventListener('click', () => {
        const i = Number(b.dataset.minus);

        if(cart[i].quantity > 1){
          cart[i].quantity--;
        } else {
          cart.splice(i, 1);
        }

        renderCart();
      })
    );

    el.querySelectorAll('[data-plus]').forEach(b =>
      b.addEventListener('click', () => {
        cart[Number(b.dataset.plus)].quantity++;
        renderCart();
      })
    );

    el.querySelectorAll('[data-remove]').forEach(b =>
      b.addEventListener('click', () => {
        cart.splice(Number(b.dataset.remove), 1);
        renderCart();
      })
    );

    const total = cart.reduce(
      (sum, x) => sum + (x.price * x.quantity),
      0
    );

    const totalEl = document.createElement('div');
    totalEl.className = 'cart-total';

    totalEl.innerHTML =
      total > 0
        ? `<strong>Total: KSh ${total.toFixed(2)}</strong>`
        : '<strong>Total confirmed before service</strong>';

    el.appendChild(totalEl);
  }

  function escapeHtml(s){
    return String(s).replace(
      /[&<>'"]/g,
      c => ({
        '&':'&amp;',
        '<':'&lt;',
        '>':'&gt;',
        "'":'&#39;',
        '"':'&quot;'
      }[c])
    );
  }

  window.chooseService = function(id){
    const s = document.getElementById('serviceSelect');

    if(s){
      s.value = String(id);
      s.scrollIntoView({
        behavior:'smooth',
        block:'center'
      });
    }
  };

  async function submitOrder(){
    const result = document.getElementById('orderResult');

    const payload = {
      customer_name: document.getElementById('orderName')?.value,
      phone: document.getElementById('orderPhone')?.value,
      delivery_area: document.getElementById('orderArea')?.value,
      notes: document.getElementById('orderNotes')?.value,
      items: cart.map(x => ({
        product_id: x.product_id,
        quantity: x.quantity
      }))
    };

    if(!payload.items.length){
      result.textContent = 'Add at least one product to your order.';
      return;
    }

    try{
      const r = await post(
        '/api/orders',
        JSON.stringify(payload),
        {'Content-Type':'application/json'}
      );

      const d = await r.json();

      if(r.ok){
        result.textContent =
          `ORDER RECEIVED — Order #${d.order_id}. Keep this order number for your payment/reference.`;

        const orderIdField = document.getElementById('orderId');
        if(orderIdField){
          orderIdField.value = d.order_id;
        }

        const paymentBox = document.getElementById('paymentReferenceBox');
        if(paymentBox){
          paymentBox.style.display = 'block';
        }

        cart.length = 0;
        renderCart();
      }else{
        result.textContent =
          d.error ||
          d.message ||
          'Request failed.';
      }

    }catch(e){
      result.textContent =
        'Network error. Please try again.';
    }
  }

  async function submitPaymentReference(){
    const result = document.getElementById('paymentReferenceResult');
    const orderId = document.getElementById('orderId')?.value;
    const phone = document.getElementById('orderPhone')?.value;
    const reference = document.getElementById('paymentReference')?.value;

    if(!orderId){
      result.textContent = 'Submit your order first to get an order number.';
      return;
    }

    if(!reference){
      result.textContent = 'Enter your M-PESA transaction reference.';
      return;
    }

    try{
      const r = await post(
        `/api/orders/${orderId}/payment-reference`,
        JSON.stringify({phone: phone, payment_reference: reference}),
        {'Content-Type':'application/json'}
      );

      const d = await r.json();
      result.textContent = d.error || d.message || 'Request failed.';
    }catch(e){
      result.textContent = 'Network error. Please try again.';
    }
  }

  async function submitService(){
    const result = document.getElementById('serviceResult');

    const payload = {
      service_id: document.getElementById('serviceSelect')?.value,
      customer_name: document.getElementById('serviceName')?.value,
      phone: document.getElementById('servicePhone')?.value,
      area: document.getElementById('serviceArea')?.value,
      description: document.getElementById('serviceDescription')?.value
    };

    try{
      const r = await post(
        '/api/service-requests',
        JSON.stringify(payload),
        {'Content-Type':'application/json'}
      );

      const d = await r.json();

      result.textContent =
        d.error ||
        d.message ||
        'Request failed.';

    }catch(e){
      result.textContent =
        'Network error. Please try again.';
    }
  }

  document
    .getElementById('submitOrder')
    ?.addEventListener('click', submitOrder);

  document
    .getElementById('submitPaymentReference')
    ?.addEventListener('click', submitPaymentReference);

  document
    .getElementById('submitService')
    ?.addEventListener('click', submitService);

  document.querySelectorAll('.add-product').forEach(b =>
    b.addEventListener('click', () =>
      addProduct(
        Number(b.dataset.id),
        b.dataset.name,
        b.dataset.price,
        b.dataset.unit
      )
    )
  );

  document.querySelectorAll('.choose-service').forEach(b =>
    b.addEventListener('click', () =>
      chooseService(Number(b.dataset.id))
    )
  );

  async function sendForm(form,url,resultId){
    const result = document.getElementById(resultId);

    try{
      const r = await post(url,new FormData(form));

      const d = await r.json().catch(() => ({
        message: r.ok ? 'Done' : 'Request failed'
      }));

      result.textContent =
        d.error ||
        d.message ||
        (r.ok ? 'Done' : 'Request failed');

      return r;

    }catch(e){
      result.textContent =
        'Network error. Please try again.';

      return null;
    }
  }

  const register = document.getElementById('register');

  if(register)
    register.addEventListener('submit', async e => {
      e.preventDefault();
      await sendForm(
        e.target,
        register.dataset.url,
        'result'
      );
    });

  const login = document.getElementById('login');

  if(login)
    login.addEventListener('submit', async e => {
      e.preventDefault();

      const r = await sendForm(
        e.target,
        login.dataset.url,
        'result'
      );

      if(r?.ok)
        location.href = '/';
    });

  const resetRequest =
    document.getElementById('reset-request');

  if(resetRequest)
    resetRequest.addEventListener('submit', async e => {
      e.preventDefault();

      await sendForm(
        e.target,
        resetRequest.dataset.url,
        'result'
      );
    });

  const resetPassword =
    document.getElementById('reset-password');

  if(resetPassword)
    resetPassword.addEventListener('submit', async e => {
      e.preventDefault();

      await sendForm(
        e.target,
        location.pathname,
        'result'
      );
    });

})();

const trackOrderButton = document.getElementById('trackOrder');

if (trackOrderButton) {
  trackOrderButton.addEventListener('click', async () => {
    const orderId = document.getElementById('trackingOrderId').value.trim();
    const phone = document.getElementById('trackingPhone').value.trim();
    const result = document.getElementById('trackingResult');

    if (!orderId || !phone) {
      result.textContent = 'Enter your order number and phone number.';
      return;
    }

    result.textContent = 'Checking order status...';

    try {
      const response = await fetch(
        `/api/orders/${encodeURIComponent(orderId)}/status?phone=${encodeURIComponent(phone)}`
      );

      const data = await response.json();

      if (!response.ok) {
        result.textContent = data.error || 'Order not found.';
        return;
      }

      const labels = {
        received: 'Received',
        confirmed: 'Confirmed',
        out_for_delivery: 'Out for delivery',
        delivered: 'Delivered',
        cancelled: 'Cancelled'
      };

      const status = labels[data.status] || data.status || 'Unknown';

      result.innerHTML = `
        <strong>Order #${data.order_id}</strong><br>
        <strong>Status:</strong> ${status}<br>
        <strong>Payment:</strong> ${data.payment_status || 'pending'}
      `;
    } catch (error) {
      result.textContent = 'Unable to check the order right now.';
    }
  });
}

/* ================= PRODUCT IMAGE VIEWER ================= */

(function () {
  const viewer = document.getElementById('product-image-viewer');
  const largeImage = document.getElementById('product-image-large');
  const imageName = document.getElementById('product-image-name');
  const download = document.getElementById('product-image-download');
  const closeTop = document.getElementById('product-image-close');
  const closeBottom = document.getElementById('product-image-close-bottom');

  if (!viewer || !largeImage || !download) return;

  function closeViewer() {
    viewer.hidden = true;
    largeImage.src = '';
    largeImage.alt = '';
    imageName.textContent = '';
    download.href = '#';
  }

  document.querySelectorAll('.product-image-button').forEach(button => {
    button.addEventListener('click', () => {
      const src = button.dataset.productImage;
      const name = button.dataset.productName || 'Product image';

      if (!src) return;

      largeImage.src = src;
      largeImage.alt = name;
      imageName.textContent = name;
      download.href = src;
      download.setAttribute('download', name.replace(/[^a-z0-9]+/gi, '-').toLowerCase() + '.jpg');

      viewer.hidden = false;
    });
  });

  closeTop.addEventListener('click', closeViewer);
  closeBottom.addEventListener('click', closeViewer);

  viewer.addEventListener('click', event => {
    if (event.target === viewer) closeViewer();
  });

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !viewer.hidden) {
      closeViewer();
    }
  });
})();
