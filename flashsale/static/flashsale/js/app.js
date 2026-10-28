(() => {
    const button = document.querySelector('#checkout-button');
    const buttonLabel = document.querySelector('#button-label');
    const nameNode = document.querySelector('#product-name');
    const descriptionNode = document.querySelector('#product-description');
    const priceNode = document.querySelector('#product-price');
    const stockNode = document.querySelector('#product-stock');
    const stockProgress = document.querySelector('#stock-progress');
    const statusNode = document.querySelector('#checkout-status');
    const checkoutIdNode = document.querySelector('#checkout-id');
    const statusPanel = document.querySelector('.status-panel');

    let product = null;
    let productLoading = false;
    let productLoadFailed = false;
    let activeCheckout = false;
    let currentAttempt = null;
    let currentCheckoutId = null;
    let purchaseConfirmed = false;
    let allowRetry = false;
    let pollTimer = null;
    let cooldownTimer = null;
    let fallbackKeyCounter = 0;

    const terminalStates = new Set(['CONFIRMED', 'REJECTED', 'FAILED']);

    function setStatus(message, state = 'neutral') {
        statusNode.textContent = message;
        statusPanel.dataset.state = state;
    }

    function syncButton() {
        const inCooldown = cooldownTimer !== null;
        button.disabled = productLoading || activeCheckout || inCooldown || purchaseConfirmed || (!product && !productLoadFailed);

        if (activeCheckout || productLoading) {
            buttonLabel.textContent = activeCheckout ? 'ORDER IN PROGRESS' : 'LOADING SALE…';
        } else if (inCooldown) {
            buttonLabel.textContent = 'PLEASE WAIT';
        } else if (purchaseConfirmed) {
            buttonLabel.textContent = 'PURCHASE CONFIRMED';
        } else if (!product && productLoadFailed) {
            buttonLabel.textContent = 'RETRY LOADING SALE';
        } else if (currentAttempt && !currentCheckoutId) {
            buttonLabel.textContent = 'RETRY SAFELY';
        } else if (product && product.available_stock <= 0 && !allowRetry) {
            button.disabled = true;
            buttonLabel.textContent = 'SOLD OUT';
        } else {
            buttonLabel.textContent = allowRetry ? 'TRY AGAIN' : 'BUY NOW';
        }
    }

    async function requestJson(url, options = {}) {
        let response;
        try {
            response = await fetch(url, {
                cache: 'no-store',
                credentials: 'same-origin',
                ...options,
                headers: {
                    Accept: 'application/json',
                    ...(options.headers || {}),
                },
            });
        } catch (_error) {
            const error = new Error('network');
            error.kind = 'network';
            throw error;
        }

        let data;
        try {
            data = await response.json();
        } catch (_error) {
            const error = new Error('invalid_json');
            error.kind = 'invalid_json';
            error.status = response.status;
            error.retryAfter = response.headers.get('Retry-After');
            throw error;
        }

        if (!response.ok) {
            const error = new Error('http_error');
            error.kind = 'http';
            error.status = response.status;
            error.data = data;
            error.retryAfter = response.headers.get('Retry-After');
            throw error;
        }
        return { response, data };
    }

    function formatPrice(value) {
        const amount = Number(value);
        if (!Number.isFinite(amount)) return '—';
        return new Intl.NumberFormat(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
        }).format(amount);
    }

    function renderProduct(item) {
        product = item;
        productLoadFailed = false;
        nameNode.textContent = item.name || 'Flash sale product';
        descriptionNode.textContent = item.description || 'A limited release, available while stock lasts.';
        priceNode.textContent = formatPrice(item.price);

        const total = Math.max(0, Number(item.total_stock) || 0);
        const available = Math.max(0, Math.min(total, Number(item.available_stock) || 0));
        stockNode.textContent = `Available: ${available} / ${total}`;
        stockProgress.max = total || 1;
        stockProgress.value = available;
        stockProgress.setAttribute('aria-label', `${available} of ${total} units available`);
        syncButton();
    }

    async function loadProduct({ quiet = false } = {}) {
        if (productLoading) return;
        productLoading = true;
        syncButton();
        if (!product && !quiet) setStatus('Loading the current sale…');

        try {
            const { data } = await requestJson('/api/products/');
            if (!Array.isArray(data)) throw new Error('invalid_product_list');
            const item = data.find((entry) => Number(entry.available_stock) > 0) || data[0];
            if (!item) {
                product = null;
                productLoadFailed = true;
                if (!quiet) setStatus('No product is available right now.', 'error');
                return;
            }
            renderProduct(item);
            if (!quiet && item.available_stock <= 0) setStatus('This sale is sold out.', 'error');
            else if (!quiet) setStatus('Sale is live. Make it yours.', 'success');
        } catch (_error) {
            product = null;
            productLoadFailed = true;
            if (!quiet) setStatus('We could not load the sale. Check your connection and try again.', 'error');
        } finally {
            productLoading = false;
            syncButton();
        }
    }

    function createIdempotencyKey() {
        if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
            return globalThis.crypto.randomUUID();
        }
        if (globalThis.crypto && typeof globalThis.crypto.getRandomValues === 'function') {
            const bytes = new Uint8Array(16);
            globalThis.crypto.getRandomValues(bytes);
            bytes[6] = (bytes[6] & 0x0f) | 0x40;
            bytes[8] = (bytes[8] & 0x3f) | 0x80;
            const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
            return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
        }
        fallbackKeyCounter += 1;
        return `sale-${Date.now().toString(36)}-${fallbackKeyCounter}-${Math.random().toString(36).slice(2)}`;
    }

    function showCheckoutId(checkoutId) {
        currentCheckoutId = checkoutId;
        checkoutIdNode.textContent = `Checkout ID: ${checkoutId}`;
        checkoutIdNode.hidden = false;
    }

    function schedulePoll(checkoutId, delay) {
        window.clearTimeout(pollTimer);
        pollTimer = window.setTimeout(() => pollCheckout(checkoutId, delay), delay);
    }

    function finishCheckout(state) {
        activeCheckout = false;
        window.clearTimeout(pollTimer);
        pollTimer = null;

        if (state === 'CONFIRMED') {
            purchaseConfirmed = true;
            allowRetry = false;
            setStatus('Purchase confirmed! Your order is complete.', 'success');
            syncButton();
            loadProduct({ quiet: true });
            return;
        }

        currentAttempt = null;
        allowRetry = true;
        if (state === 'REJECTED') {
            setStatus('Sorry, the item is sold out. You can try again.', 'error');
            loadProduct({ quiet: true });
        } else {
            setStatus("We couldn't complete the order. You can try again.", 'error');
        }
        syncButton();
    }

    async function pollCheckout(checkoutId, previousDelay = 750) {
        if (!activeCheckout || currentCheckoutId !== checkoutId) return;

        try {
            const { data } = await requestJson(`/api/checkout/${encodeURIComponent(checkoutId)}/`);
            const state = data && data.status;
            if (!['QUEUED', 'PROCESSING', ...terminalStates].includes(state)) {
                throw new Error('invalid_checkout_status');
            }

            if (state === 'QUEUED') setStatus('Your order is in the queue.', 'active');
            else if (state === 'PROCESSING') setStatus('Your order is being processed.', 'active');
            else {
                finishCheckout(state);
                return;
            }
            schedulePoll(checkoutId, 750);
        } catch (_error) {
            setStatus('Order status is temporarily unavailable. We are still checking.', 'active');
            schedulePoll(checkoutId, Math.min(Math.max(previousDelay * 2, 1000), 8000));
        }
    }

    function beginCooldown(retryAfter) {
        const parsed = Number.parseInt(retryAfter, 10);
        const seconds = Number.isFinite(parsed) && parsed > 0 ? parsed : 5;
        currentAttempt = null;
        activeCheckout = false;
        allowRetry = false;
        cooldownTimer = window.setTimeout(() => {
            cooldownTimer = null;
            setStatus('You can try again now.');
            syncButton();
        }, seconds * 1000);
        setStatus(`Too many attempts. Please wait ${seconds} seconds before trying again.`, 'error');
        syncButton();
    }

    function handleCheckoutError(error) {
        activeCheckout = false;
        if (error.status === 429) {
            beginCooldown(error.retryAfter);
            return;
        }
        if (error.status === 409 && error.data && error.data.checkout_id) {
            showCheckoutId(error.data.checkout_id);
            finishCheckout('REJECTED');
            return;
        }
        if (error.status && error.status >= 400 && error.status < 500 && error.status !== 429) {
            currentAttempt = null;
            allowRetry = true;
            setStatus('The request was not accepted. Please check the sale and try again.', 'error');
            syncButton();
            return;
        }

        // A lost/invalid response may follow a successful commit. Retrying with
        // the same key lets the server return that checkout instead of duplicating it.
        setStatus('We could not confirm the response. Retry safely to check the same order.', 'error');
        syncButton();
    }

    async function startCheckout() {
        if (activeCheckout || productLoading || cooldownTimer || purchaseConfirmed) return;
        if (!product) {
            await loadProduct();
            return;
        }

        if (!currentAttempt) {
            currentAttempt = { key: createIdempotencyKey() };
            currentCheckoutId = null;
            checkoutIdNode.hidden = true;
        }
        activeCheckout = true;
        allowRetry = false;
        setStatus(currentAttempt.checkoutId ? 'Checking your existing order…' : 'Submitting your order…', 'active');
        syncButton();

        try {
            const { response, data } = await requestJson('/api/checkout/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    product_id: product.id,
                    idempotency_key: currentAttempt.key,
                }),
            });
            if (![200, 202].includes(response.status) || !data || data.checkout_id === undefined) {
                throw new Error('invalid_checkout_response');
            }
            currentAttempt.checkoutId = data.checkout_id;
            showCheckoutId(data.checkout_id);
            setStatus('Your order is queued.', 'active');
            schedulePoll(data.checkout_id, 650);
        } catch (error) {
            handleCheckoutError(error);
        }
    }

    button.addEventListener('click', startCheckout);
    loadProduct();
})();
