using System.Collections;
using System.Collections.Generic;
using TMPro;
using UnityEngine;
using UnityEngine.UI;

public class Hierarchy : MonoBehaviour
{

    public GameObject itemPrefab;
    public Transform contentTransform;

    List<HierarchyItem> items;

    UIManager manager;

    // Start is called before the first frame update
    void Start()
    {
        items = new List<HierarchyItem>();

        manager = FindObjectOfType<UIManager>();
    }

    // Update is called once per frame
    void Update()
    {
    }

    public void AddItem(GameObject item){
        GameObject newItem = Instantiate(itemPrefab, contentTransform);
        newItem.name=item.name;
        newItem.GetComponent<RectTransform>().anchoredPosition=new Vector3(5,items.Count*-30,0);
        HierarchyItem hierarchyItem = newItem.GetComponent<HierarchyItem>();
        hierarchyItem.selectButton.GetComponentInChildren<TMP_Text>().text=item.name;
        hierarchyItem.selectButton.onClick.AddListener(() => Select(item));
        hierarchyItem.muteButton.GetComponent<UIMenu_Mute>().item=item;
        hierarchyItem.soloButton.GetComponent<UIMenu_Solo>().item=item;
        hierarchyItem.item=item;
        items.Add(hierarchyItem);
    }

    public void RemoveItem(GameObject go){
        HierarchyItem toRemove = items.Find(x => x.item==go);
        items.Remove(toRemove);
        Destroy(toRemove.gameObject);
        ReorderItems();
    }

    void ReorderItems(){
        for (int i = 0; i < items.Count; i++)
        {
            items[i].GetComponent<RectTransform>().anchoredPosition=new Vector3(5,i*-30,0);
        }
    }

    void Select(GameObject item, bool forceMultipleSelection=false){
        bool multipleSelection = Input.GetAxis("MultipleSelection")==1 || forceMultipleSelection;        
        if (manager.selected.Contains(item)){
            if (!multipleSelection){
                manager.DeselectAll();
                manager.AddToSelection(item);
            }
            else{
                manager.Deselect(item);
            }
        }
        else{
            if (!multipleSelection){
                manager.DeselectAll();
            }
            manager.AddToSelection(item);
        }
    }
}
