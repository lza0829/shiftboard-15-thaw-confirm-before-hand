<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const shifts = ref([])
const fromId = ref(1)
const toId = ref(2)
const current = ref(null)
const msg = ref('')
const ok = ref(false)

const TAG_CLASS = {
  '在途': 'st-in',
  '冻结': 'st-frozen',
  '待确认归还': 'st-pending',
  '已结清': 'st-done',
  '已报废': 'st-scrap',
}

function tagClass(status) {
  return TAG_CLASS[status] || ''
}

async function refresh() {
  shifts.value = await getJson('/api/shifts')
  const logs = await getJson('/api/handovers')
  current.value = logs.find((h) => h.status === '进行中') || null
  if (current.value) {
    current.value = await getJson(`/api/handovers/${current.value.id}`)
  }
}

onMounted(async () => {
  try {
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
})

async function start() {
  msg.value = ''
  ok.value = false
  try {
    current.value = await postJson('/api/handovers/start', {
      from_shift_id: Number(fromId.value),
      to_shift_id: Number(toId.value),
    })
    ok.value = true
    const excluded = (current.value.excluded || []).length
    msg.value = `已开交班单 #${current.value.id}，清单 ${current.value.batch_count} 批`
      + (excluded ? `，排除 ${excluded} 批（冻结/待确认归还）` : '')
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
}

async function complete() {
  if (!current.value) return
  msg.value = ''
  ok.value = false
  try {
    current.value = await postJson(`/api/handovers/${current.value.id}/complete`)
    ok.value = current.value.status === '已完成'
    msg.value = current.value.status === '已完成'
      ? `交班单 #${current.value.id} 已完成，批次已过户`
      : `交班单 #${current.value.id} 失败：${current.value.reason || '归属不一致'}`
    await refresh()
  } catch (e) {
    msg.value = String(e.message || e)
  }
}
</script>

<template>
  <div>
    <h1>交班办理</h1>
    <p class="lead">同一时刻只允许一单进行中的交班；清单内在途批次必须整批过户，冻结/待确认归还批次发起时即排除并留痕。</p>

    <div class="panel">
      <div class="row">
        <label>交班方
          <select v-model.number="fromId">
            <option v-for="s in shifts" :key="s.id" :value="s.id">{{ s.name }} · {{ s.keeper }}</option>
          </select>
        </label>
        <label>接班方
          <select v-model.number="toId">
            <option v-for="s in shifts" :key="'t'+s.id" :value="s.id">{{ s.name }} · {{ s.keeper }}</option>
          </select>
        </label>
        <button class="btn" :disabled="!!current" @click="start">发起交班</button>
        <button class="btn ghost" :disabled="!current" @click="complete">完成交班</button>
      </div>
      <p class="msg" :class="{ ok }">{{ msg }}</p>
    </div>

    <div class="panel" v-if="current">
      <h2>当前交班单 #{{ current.id }} · {{ current.status }}</h2>
      <p class="lead" style="margin-bottom:12px">
        {{ current.from_shift_name }} → {{ current.to_shift_name }} · 清单 {{ current.batch_count }} 批
      </p>
      <table>
        <thead><tr><th>业务键</th><th>标题</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="b in current.items || []" :key="b.id">
            <td>{{ b.biz_key }}</td>
            <td>{{ b.title }}</td>
            <td><span class="tag" :class="tagClass(b.status)">{{ b.status }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="panel" v-if="current && (current.excluded || []).length">
      <h2>本次排除 · 不参与交班</h2>
      <p class="lead" style="margin-bottom:12px">排除结果已在发起快照中固化；排除原因与批次状态同源。</p>
      <table>
        <thead><tr><th>业务键</th><th>标题</th><th>快照时状态</th><th>排除原因</th></tr></thead>
        <tbody>
          <tr v-for="x in current.excluded" :key="x.batch_id">
            <td>{{ x.biz_key }}</td>
            <td>{{ x.title }}</td>
            <td><span class="tag" :class="tagClass(x.status_at_snapshot)">{{ x.status_at_snapshot }}</span></td>
            <td>{{ x.exclude_reason }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
