const CACHE_NAME = 'smart-reader-v1.0.1';
const STATIC_ASSETS = [
  '/icon-192.png',
  '/icon-512.png'
];

// Устанавливаем сразу, не ждём
self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
  );
});

// Активируем сразу, удаляем старые кэши
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(names => {
      return Promise.all(
        names.filter(name => name !== CACHE_NAME).map(name => caches.delete(name))
      );
    }).then(() => self.clients.claim())
  );
});

// Стратегия: HTML всегда из сети, статика из кэша
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  
  // HTML и API — всегда из сети
  if (event.request.mode === 'navigate' || url.pathname.startsWith('/api')) {
    event.respondWith(
      fetch(event.request).catch(() => caches.match('/index.html'))
    );
    return;
  }
  
  // Статика — из кэша
  event.respondWith(
    caches.match(event.request).then(cached => {
      return cached || fetch(event.request).then(response => {
        // Кэшируем новые файлы
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      });
    })
  );
});

// Проверяем обновления каждые 5 минут
self.addEventListener('message', event => {
  if (event.data === 'CHECK_UPDATE') {
    self.registration.update();
  }
});
