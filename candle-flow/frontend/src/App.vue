<script setup lang="ts">
import { onMounted } from 'vue'
import Header from '@/components/Header.vue'
import Footer from '@/components/Footer.vue'
import { useConfigStore } from '@/stores/config'
import { useWatchlistStore } from '@/stores/watchlist'

const config = useConfigStore()
const watchlist = useWatchlistStore()
onMounted(async () => {
  document.documentElement.setAttribute('data-theme', config.theme)
  await config.restoreSession()
  await watchlist.load()
})
</script>

<template>
  <div class="app-layout">
    <Header />
    <main class="main-content">
      <RouterView />
    </main>
    <Footer />
  </div>
</template>

<style scoped>
.app-layout {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
.main-content {
  flex: 1;
  max-width: 1400px;
  width: 100%;
  margin: 0 auto;
  padding: var(--space-lg);
}
@media (max-width: 768px) {
  .main-content {
    padding: var(--space-sm) var(--space-md);
  }
}
</style>
