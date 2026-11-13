function setupVenueSearch(){
  const input=document.querySelector('[data-venue-search]');
  if(!input)return;
  const results=document.getElementById('venue-results');
  const picked=document.getElementById('venue-picked');
  const venueId=document.getElementById('venue-id');
  const venueName=document.getElementById('restaurant-name');
  let timer;

  function chooseVenue(id,name){
    venueId.value=id;
    venueName.value=name;
    input.value=name;
    picked.textContent='✓ '+name;
    picked.style.display='block';
    results.style.display='none';
  }

  input.addEventListener('input',()=>{
    clearTimeout(timer);
    const q=input.value.trim();
    venueId.value='';
    venueName.value='';
    picked.style.display='none';
    if(q.length<2){results.style.display='none';return}

    timer=setTimeout(async()=>{
      try{
        const r=await fetch('/api/venues/search?q='+encodeURIComponent(q));
        const items=await r.json();
        results.innerHTML='';
        items.forEach(v=>{
          const el=document.createElement('div');
          el.className='search-item';
          el.innerHTML='<b></b><small></small>';
          el.querySelector('b').textContent=v.name;
          el.querySelector('small').textContent=[v.neighborhood,v.cuisine].filter(Boolean).join(' · ');
          el.onclick=()=>chooseVenue(v.id,v.name);
          results.appendChild(el);
        });
        results.style.display=items.length?'block':'none';
      }catch(e){results.style.display='none'}
    },220);
  });

  document.querySelectorAll('[data-saved-venue]').forEach(b=>{
    b.addEventListener('click',()=>chooseVenue(b.dataset.venueId,b.dataset.venueName));
  });
}

function setupTimeBricks(){
  const hidden=document.getElementById('preferred-time');
  if(!hidden)return;
  const label=document.getElementById('selected-time-label');
  const scroller=document.getElementById('time-scroller');
  const bricks=[...document.querySelectorAll('[data-time-value]')];
  const custom=document.getElementById('custom-time-input');

  function select(value,text,brick=null){
    hidden.value=value;
    label.textContent=text;
    bricks.forEach(b=>b.classList.remove('selected'));
    if(brick)brick.classList.add('selected');
  }

  bricks.forEach(b=>{
    b.addEventListener('click',()=>select(b.dataset.timeValue,b.dataset.timeLabel,b));
  });

  if(custom){
    custom.addEventListener('change',()=>{
      if(!custom.value)return;
      const [h,m]=custom.value.split(':').map(Number);
      const d=new Date(2000,0,1,h,m);
      const text=d.toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
      select(custom.value,text,null);
    });
  }

  const selected=document.querySelector('.time-brick.selected');
  if(selected&&scroller){
    requestAnimationFrame(()=>selected.scrollIntoView({behavior:'instant',inline:'center',block:'nearest'}));
  }
}

document.addEventListener('DOMContentLoaded',()=>{
  setupVenueSearch();
  setupTimeBricks();
});
