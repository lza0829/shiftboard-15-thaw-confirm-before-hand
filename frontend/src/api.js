async function toError(res) {
  const text = await res.text()
  try {
    const data = JSON.parse(text)
    if (data && data.detail) return new Error(data.detail)
  } catch { /* 非 JSON 响应，原样透出 */ }
  return new Error(text)
}

export async function getJson(url) {
  const res = await fetch(url)
  if (!res.ok) throw await toError(res)
  return res.json()
}

export async function postJson(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  if (!res.ok) throw await toError(res)
  return res.json()
}
