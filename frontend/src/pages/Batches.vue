<script setup>
import { onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const rows = ref([])
const err = ref('')
const msg = ref('')
const eventsFor = ref(null)
const events = ref([])

// 状态机：解冻只进「待确认归还」，确认归还后才回在途
const ACTIONS = {
  '在途': [
    { key: 'freeze', label: '冻结' },
    { key: 'settle', label: '结清' },
  ],
  '冻结': [
    { key: 'unfreeze', label: '解冻' },
    { key: 'scrap', label: '报废', danger: true },
  ],
  '待确认归还': [
    { key: 'confirm-return', label: '确认归还' },
    { key: 'refreeze', label: '再冻结' },
    { key: 'scrap', label: '报废', danger: true },
  ],
}

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
  rows.value = await getJson('/api/batches')
}

async function act(b, action) {
  err.value = ''
  msg.value = ''
  try {
    // 无登录态，操作班次即批次当前责任班次，后端仍会校验一致性
    await postJson(`/api/batches/${b.id}/${action.key}`, {
      operator_shift_id: b.owner_shift_id,
    })
    msg.value = `${b.biz_key} 已执行「${action.label}」`
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function toggleEvents(b) {
  err.value = ''
  if (eventsFor.value === b.id) {
    eventsFor.value = null
    return
  }
  try {
    events.value = await getJson(`/api/batches/${b.id}/events`)
    eventsFor.value = b.id
  } catch (e) {
    err.value = String(e.message || e)
  }
}

onMounted(async () => {
  try {
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <div>
    <h1>在途批次</h1>
    <p class="lead">
      在途 / 冻结 / 待确认归还三态分列；解冻只进「待确认归还」，须责任班次确认归还后才可交班。
    </p>
    <p v-if="err" class="msg">{{ err }}</p>
    <p v-if="msg" class="msg ok">{{ msg }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr><th>业务键</th><th>标题</th><th>归属班次</th><th>状态</th><th>备注</th><th>操作</th></tr>
        </thead>
        <tbody>
          <template v-for="b in rows" :key="b.id">
            <tr>
              <td>{{ b.biz_key }}</td>
              <td>{{ b.title }}</td>
              <td>{{ b.owner_shift_name || '—' }}</td>
              <td><span class="tag" :class="tagClass(b.status)">{{ b.status }}</span></td>
              <td>{{ b.note || '—' }}</td>
              <td>
                <div class="actions">
                  <button
                    v-for="a in ACTIONS[b.status] || []"
                    :key="a.key"
                    class="btn small ghost"
                    :class="{ danger: a.danger }"
                    @click="act(b, a)"
                  >{{ a.label }}</button>
                  <button class="btn small ghost" @click="toggleEvents(b)">
                    {{ eventsFor === b.id ? '收起' : '审计' }}
                  </button>
                </div>
              </td>
            </tr>
            <tr v-if="eventsFor === b.id" class="events-row">
              <td colspan="6">
                <table class="events-table">
                  <thead>
                    <tr><th>时间</th><th>动作</th><th>从</th><th>到</th><th>操作班次</th><th>原因</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="e in events" :key="e.id">
                      <td>{{ e.created_at }}</td>
                      <td>{{ e.action }}</td>
                      <td>{{ e.from_status }}</td>
                      <td>{{ e.to_status }}</td>
                      <td>{{ e.operator_shift_name || '—' }}</td>
                      <td>{{ e.reason || '—' }}</td>
                    </tr>
                    <tr v-if="!events.length"><td colspan="6">暂无状态变迁记录</td></tr>
                  </tbody>
                </table>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </div>
</template>
