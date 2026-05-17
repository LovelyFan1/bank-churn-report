import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'

export const useAppStore = defineStore('app', () => {
  const overview = ref(null)
  const loading = ref(false)

  async function fetchOverview() {
    loading.value = true
    try {
      const { data } = await api.get('/data/overview')
      overview.value = data
    } finally {
      loading.value = false
    }
  }

  return { overview, loading, fetchOverview }
})
