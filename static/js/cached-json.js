// Bounded per-page cache. Revalidate with ETag; 304 carries no library body.
const cache = new Map();
export async function cachedJson(url, options = {}, fetcher = fetch) {
  const saved = cache.get(url);
  const response = await fetcher(url, {...options, cache: 'no-cache',
    headers: {...options.headers, ...(saved?.etag ? {'If-None-Match': saved.etag} : {})}});
  if (response.status === 304 && saved) return saved.value;
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail || '读取失败');
  cache.delete(url);
  cache.set(url, {etag: response.headers.get('ETag'), value});
  if (cache.size > 32) cache.delete(cache.keys().next().value);
  return value;
}
